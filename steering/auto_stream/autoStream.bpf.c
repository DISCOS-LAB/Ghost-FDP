#include <linux/nvme.h>
#include <linux/types.h>
#include <linux/blk-mq.h>
#include <linux/blkdev.h>
#include <linux/blk_types.h>

#define __LBA_SHIFT__ 0        // injected: 9 (512B) or 12 (4K)
#define __LBAS_PER_CHUNK__ 0   // injected: chunk_size / logical_block_size
#define FDP_PI_NUM 0           // injected
#define __LOG_LUT_MAX__ 0      // injected: max access_cnt for log LUT
#define __DECAY_PERIOD_SEC__ 0 // injected: SFR decay period (seconds)
#define __DUMP_PI_COUNTS__ 0
#define SECTOR_SHIFT 9
#define ACCESS_CNT_MAX __LOG_LUT_MAX__

struct chunk_info {
	u64 last_access_ts_sec;
	u64 last_accessed_lba;
	u32 access_cnt; /* SFR adjusted count, capped at ACCESS_CNT_MAX */
#if __DUMP_PI_COUNTS__
	u32 pi_write_count[FDP_PI_NUM];
#endif
	u8 stream_id; /* 1-based NVMe bi_write_stream; 0 = unset */
};

#define __MAX_CHUNKS__ 0
#define __TRACK_BD_MAJOR__ 0
#define __TRACK_BD_MINOR__ 0

#ifndef MINORBITS
#define MINORBITS 20
#endif
#ifndef MINORMASK
#define MINORMASK ((1U << MINORBITS) - 1)
#endif

#ifndef BPF_PROBE_WRITE_KERNEL_BIO_WRITE_STREAM_ID
#define BPF_PROBE_WRITE_KERNEL_BIO_WRITE_STREAM_ID 212
#endif
static long (*bpf_probe_write_kernel_bio_write_stream)(struct bio *bio, u8 stream_id) =
	(void *)BPF_PROBE_WRITE_KERNEL_BIO_WRITE_STREAM_ID;

BPF_ARRAY(chunk_array, struct chunk_info, __MAX_CHUNKS__);
/* key = access_cnt (1..__LOG_LUT_MAX__), value = stream index 0..FDP_PI_NUM-1 */
BPF_ARRAY(log_lut, u8, __LOG_LUT_MAX__ + 1);

static __always_inline u32 sfr_apply_recency(u32 access_cnt, u64 delta_sec)
{
	u32 n;
	u32 decay_sec = __DECAY_PERIOD_SEC__ ? __DECAY_PERIOD_SEC__ : 1;

	/* Full decay periods elapsed -> divide by 2^N (paper: pow(2, dt/decay)). */
	n = (u32)(delta_sec / (u64)decay_sec);
	if (n > 30)
		n = 30;

	if (access_cnt < ACCESS_CNT_MAX)
		access_cnt += 1;
	else
		access_cnt = ACCESS_CNT_MAX;

	access_cnt >>= n;
	if (access_cnt == 0)
		access_cnt = 1;
	if (access_cnt > ACCESS_CNT_MAX)
		access_cnt = ACCESS_CNT_MAX;
	return access_cnt;
}

static __always_inline u8 sfr_stream_from_access_cnt(u32 access_cnt)
{
	u32 key = access_cnt;
	u8 *sid_p;

	if (key > ACCESS_CNT_MAX)
		key = ACCESS_CNT_MAX;
	sid_p = log_lut.lookup(&key);
	if (!sid_p)
		return 0;
	if (*sid_p >= FDP_PI_NUM)
		return (u8)(FDP_PI_NUM - 1);
	return *sid_p;
}

/*
 * fentry on nvme_core.nvme_setup_cmd.
 * seq: reuse stream. non-seq: SFR access_cnt / 2^(dt/decay) then log10 LUT.
 */
MODULE_KFUNC_PROBE(nvme_core, nvme_setup_cmd, void *ns, struct request *req) {
	u64 now_sec = bpf_ktime_get_ns() / 1000000000ULL;

	(void)ns;
	if (!req)
		return 0;

	struct bio *bio = req->bio;
	if (!bio)
		return 0;
	struct block_device *bdev = bio->bi_bdev;
	if (!bdev)
		return 0;
	{
		u32 bd_dev = bdev->bd_dev;
		unsigned int maj = bd_dev >> MINORBITS;
		unsigned int min = bd_dev & MINORMASK;
		if (maj != (unsigned int)__TRACK_BD_MAJOR__ || min != (unsigned int)__TRACK_BD_MINOR__)
			return 0;
	}

	if (req_op(req) != REQ_OP_WRITE)
		return 0;

	u64 sector = blk_rq_pos(req);
	u32 bytes = blk_rq_bytes(req);
	u64 slba = sector >> (__LBA_SHIFT__ - SECTOR_SHIFT);
	u64 nlb_count = bytes >> __LBA_SHIFT__;
	u64 end_lba = slba + nlb_count;

	u32 chunk_idx = slba / __LBAS_PER_CHUNK__;
	if (chunk_idx >= __MAX_CHUNKS__)
		return 0;

	struct chunk_info *info = chunk_array.lookup(&chunk_idx);
	if (!info)
		return 0;

	bool is_sequential =
		(info->last_accessed_lba != 0 &&
		 info->last_accessed_lba != (u64)-1 &&
		 slba == info->last_accessed_lba);

	info->last_accessed_lba = end_lba;

	if (is_sequential) {
		u8 sid = info->stream_id < 1 ? 1 : info->stream_id;
		u32 pi = (u32)(sid - 1);
		if (pi >= FDP_PI_NUM)
			pi = FDP_PI_NUM - 1;
#if __DUMP_PI_COUNTS__
		if (info->pi_write_count[pi] != (u32)-1)
			info->pi_write_count[pi] += 1;
#endif
		long seq_write_ret = bpf_probe_write_kernel_bio_write_stream(bio, sid);
		if (seq_write_ret < 0)
			bpf_trace_printk("Write Failed! Error: %d seq slba: %llu bytes: %u\n",
					 (int)seq_write_ret, slba, bytes);
		return 0;
	}

	if (info->last_access_ts_sec == 0) {
		info->access_cnt = 1;
	} else {
		u64 delta_sec = now_sec - info->last_access_ts_sec;
		info->access_cnt = sfr_apply_recency(info->access_cnt, delta_sec);
	}
	info->last_access_ts_sec = now_sec;

	{
		u8 fdp_pi = sfr_stream_from_access_cnt(info->access_cnt);
		u8 sid = (u8)(fdp_pi + 1);

#if __DUMP_PI_COUNTS__
		if (info->pi_write_count[fdp_pi] != (u32)-1)
			info->pi_write_count[fdp_pi] += 1;
#endif
		info->stream_id = sid;
		long write_ret = bpf_probe_write_kernel_bio_write_stream(bio, sid);
		if (write_ret < 0)
			bpf_trace_printk("Write Failed! Error: %d slba: %llu nlb: %llu\n",
					 (int)write_ret, slba, nlb_count);
	}
	return 0;
}

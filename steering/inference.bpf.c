#include <linux/nvme.h>
#include <linux/types.h>
#include <linux/blk-mq.h>
#include <linux/blkdev.h>
#include <linux/blk_types.h>

#define __LBA_SHIFT__ 0        // injected: 9 (512B) or 12 (4K)
#define __LBAS_PER_CHUNK__ 0   // injected: chunk_size / logical_block_size
#define UPDATE_COUNT_MAX 0xFFFFU
#define UPDATE_COUNT_CAP (UPDATE_COUNT_MAX - 1U)
#define FDP_PI_NUM 0           // injected
#define PI_THRESHOLD_NUM (FDP_PI_NUM - 1)
#define __LOG1P_LUT_MAX__ 0    // injected: 65535
#define __WINDOW_SEC__ 0       // injected: window_size
#define __LOG_WINDOW_X10__ 0   // injected: round(log(window_sec) * 10)
#define __COEF_UPD_FREQ_X100__ 0
#define __COEF_LAST_INT_S_LOG1P_X100__ 0
#define __HAZARD_SCORE_OFFSET_X100__ 0
#define __DUMP_PI_COUNTS__ 0
#define __HAZARD_HIST_BINS__ 0
#define SECTOR_SHIFT 9

struct global_config {
	u64 window_start_ms;
	u64 window_end_ms;
	u32 window_id;
	u32 pi_thresholds_x100[PI_THRESHOLD_NUM];
};

struct chunk_info {
	u64 last_accessed_lba;
	u64 last_write_ms; /* last non-seq write; 0 = never */
	u32 hazard_score_x100;
	u32 window_id;
#if __DUMP_PI_COUNTS__
	u32 pi_write_count[FDP_PI_NUM];
#endif
	u16 update_count;
	u8 stream_id;
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
BPF_ARRAY(log1p_lut_x10, u16, __LOG1P_LUT_MAX__ + 1);
BPF_ARRAY(hazard_hist, u64, __HAZARD_HIST_BINS__);
BPF_ARRAY(g_cfg_map, struct global_config, 1);

/*
 * fentry on nvme_core.nvme_setup_cmd.
 * seq / elapsed==0 / first non-seq: reuse stream.
 * hazard = coef1*(log1p(uc)+log(r)-log1p(elapsed)) + coef2*log1p(last_int_s) + offset
 * PI: uniform cuts on [p1, p99] from userspace.
 */
MODULE_KFUNC_PROBE(nvme_core, nvme_setup_cmd, void *ns, struct request *req) {
	u64 now_ms = bpf_ktime_get_ns() / 1000000ULL;
	u32 zero_key = 0;
	struct global_config *g_cfg = g_cfg_map.lookup(&zero_key);
	if (!g_cfg)
		return 0;

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

	if (g_cfg->window_start_ms || g_cfg->window_end_ms) {
		if (now_ms <= g_cfg->window_start_ms || now_ms >= g_cfg->window_end_ms) {
			u8 sid = info->stream_id < 1 ? 1 : info->stream_id;
			bpf_probe_write_kernel_bio_write_stream(bio, sid);
			return 0;
		}
	}

	if (info->window_id != g_cfg->window_id) {
		info->update_count = 0;
		info->window_id = g_cfg->window_id;
	}

	bool is_sequential =
		(info->last_accessed_lba != (u64)-1 && slba == info->last_accessed_lba);

	u64 elapsed_ms = 0;
	if (now_ms >= g_cfg->window_start_ms)
		elapsed_ms = now_ms - g_cfg->window_start_ms;
	u32 elapsed_sec = (u32)(elapsed_ms / 1000ULL);

	if (is_sequential || elapsed_sec == 0 || info->last_write_ms == 0) {
		if (!is_sequential) {
			if (info->update_count >= UPDATE_COUNT_CAP)
				info->update_count = UPDATE_COUNT_CAP;
			else
				info->update_count += 1;
			info->last_write_ms = now_ms;
		}
		info->last_accessed_lba = end_lba;
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

	u64 last_int_ms = 0;
	if (info->last_write_ms != 0 && now_ms >= info->last_write_ms)
		last_int_ms = now_ms - info->last_write_ms;
	u32 last_int_s = (u32)(last_int_ms / 1000ULL);

	if (info->update_count >= UPDATE_COUNT_CAP)
		info->update_count = UPDATE_COUNT_CAP;
	else
		info->update_count += 1;
	info->last_accessed_lba = end_lba;
	info->last_write_ms = now_ms;

	if (elapsed_sec > (u32)__WINDOW_SEC__)
		elapsed_sec = (u32)__WINDOW_SEC__;

	u32 uc_key = info->update_count;
	if (uc_key > (u32)__LOG1P_LUT_MAX__)
		uc_key = (u32)__LOG1P_LUT_MAX__;
	u16 *uc_log1p_p = log1p_lut_x10.lookup(&uc_key);
	u16 uc_log1p_x10 = uc_log1p_p ? *uc_log1p_p : 0;

	u32 elapsed_log_key = elapsed_sec - 1;
	u16 *elapsed_log_p = log1p_lut_x10.lookup(&elapsed_log_key);
	u16 elapsed_log_x10 = elapsed_log_p ? *elapsed_log_p : 0;

	u32 last_int_key = (last_int_s > (u32)__WINDOW_SEC__)
				   ? (u32)__WINDOW_SEC__
				   : last_int_s;
	u16 *last_int_log1p_p = log1p_lut_x10.lookup(&last_int_key);
	u16 last_int_log1p_x10 = last_int_log1p_p ? *last_int_log1p_p : 0;

	/* BPF rejects signed division: abs(prod)/10 then restore sign. */
	u64 feat_upd_x10 = (u64)uc_log1p_x10 + (u64)__LOG_WINDOW_X10__;
	if (feat_upd_x10 < (u64)elapsed_log_x10)
		feat_upd_x10 = 0;
	else
		feat_upd_x10 -= (u64)elapsed_log_x10;
	u64 term1_x100 =
		((u64)__COEF_UPD_FREQ_X100__ * feat_upd_x10) / 10ULL;
	s32 coef_last = __COEF_LAST_INT_S_LOG1P_X100__;
	u64 coef_last_abs = coef_last < 0 ? (u64)(-coef_last) : (u64)coef_last;
	u64 term2_mag =
		(coef_last_abs * (u64)last_int_log1p_x10) / 10ULL;
	s64 term2_x100 = coef_last < 0 ? -(s64)term2_mag : (s64)term2_mag;
	s64 acc = (s64)term1_x100 + term2_x100
		+ (s64)__HAZARD_SCORE_OFFSET_X100__;
	u32 hazard_score_x100 = acc <= 0 ? 0 : (u32)acc;
	info->hazard_score_x100 = hazard_score_x100;

	{
		u32 hist_key = hazard_score_x100;
		if (hist_key >= (u32)__HAZARD_HIST_BINS__)
			hist_key = (u32)__HAZARD_HIST_BINS__ - 1;
		u64 *hist_slot = hazard_hist.lookup(&hist_key);
		if (hist_slot)
			__sync_fetch_and_add(hist_slot, 1);
	}

	u32 fdp_pi = 0;
#pragma unroll
	for (u32 i = 0; i < PI_THRESHOLD_NUM; i++) {
		if (hazard_score_x100 > g_cfg->pi_thresholds_x100[i])
			fdp_pi++;
		else
			break;
	}
	if (fdp_pi >= FDP_PI_NUM)
		fdp_pi = FDP_PI_NUM - 1;

#if __DUMP_PI_COUNTS__
	if (info->pi_write_count[fdp_pi] != (u32)-1)
		info->pi_write_count[fdp_pi] += 1;
#endif

	info->stream_id = (u8)(fdp_pi + 1);
	long write_ret = bpf_probe_write_kernel_bio_write_stream(bio, info->stream_id);
	if (write_ret < 0)
		bpf_trace_printk("Write Failed! Error: %d slba: %llu nlb: %llu\n",
				 (int)write_ret, slba, nlb_count);
	return 0;
}

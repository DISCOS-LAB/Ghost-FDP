#include <linux/nvme.h>
#include <linux/types.h>
#include <linux/blk-mq.h>
#include <linux/blkdev.h>
#include <linux/blk_types.h>

#define __LBA_SHIFT__ 0        // injected: 9 (512B) or 12 (4K)
#define __LBAS_PER_CHUNK__ 0   // injected: chunk_size / logical_block_size
#define UPDATE_COUNT_MAX 0xFFFFFFFFU
#define __SV_TAU_MS__ 0        // injected: sv_tau_sec * 1000
#define SECTOR_SHIFT 9

/* CLOCK_MONOTONIC ms (bpf_ktime_get_ns() / 1e6). */
struct global_config {
	u64 window_start_ms;
	u64 window_end_ms;
	u32 window_id;
};

struct chunk_info {
	u64 last_accessed_lba;
	u64 last_write_ms; /* absolute monotonic ms of last non-seq write; persists; 0=never */
	u32 last_int_ms; /* recency at τ in ms (τ - last_write); frozen on first post-τ write */
	u32 sv_time2event_ms; /* ms from τ to first post-τ non-seq write */
	u32 window_id;
	u32 update_count; /* non-seq writes in [window_start, τ] (= frequency) */
	u8 sv_event_indicator; /* 1 = non-seq write after τ before window end */
};

#define __MAX_CHUNKS__ 0 // injected
#define __TRACK_BD_MAJOR__ 0
#define __TRACK_BD_MINOR__ 0

#ifndef MINORBITS
#define MINORBITS 20
#endif
#ifndef MINORMASK
#define MINORMASK ((1U << MINORBITS) - 1)
#endif

BPF_ARRAY(chunk_array, struct chunk_info, __MAX_CHUNKS__);
BPF_ARRAY(g_cfg_map, struct global_config, 1);

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

	if (g_cfg->window_start_ms || g_cfg->window_end_ms) {
		if (now_ms <= g_cfg->window_start_ms || now_ms >= g_cfg->window_end_ms)
			return 0;
	}

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

	if (info->window_id != g_cfg->window_id) {
		info->update_count = 0;
		info->sv_event_indicator = 0;
		info->sv_time2event_ms = 0;
		info->last_int_ms = 0;
		info->window_id = g_cfg->window_id;
	}

	bool is_sequential =
		(info->last_accessed_lba != (u64)-1 && slba == info->last_accessed_lba);

	/* Sequential: extend LBA chain only; features count non-seq access episodes. */
	if (is_sequential) {
		info->last_accessed_lba = end_lba;
		return 0;
	}

	u64 tau_abs = g_cfg->window_start_ms + (u64)__SV_TAU_MS__;

	if (now_ms <= tau_abs) {
		/* At/before τ: frequency + last_write only (no future leakage). */
		if (info->update_count < UPDATE_COUNT_MAX)
			info->update_count += 1;
		info->last_write_ms = now_ms;
	} else if (info->last_write_ms == 0) {
		/* First non-seq ever: plant last_write; skip event (no last_int). */
		info->last_write_ms = now_ms;
	} else {
		/* After τ: freeze recency once (while last_write still pre-τ), then event. */
		if (!info->sv_event_indicator) {
			info->sv_event_indicator = 1;
			info->sv_time2event_ms = (u32)(now_ms - tau_abs);
			if (tau_abs >= info->last_write_ms)
				info->last_int_ms = (u32)(tau_abs - info->last_write_ms);
			else
				info->last_int_ms = 0;
		}
		/* Keep last_write_ms current for subsequent windows. */
		info->last_write_ms = now_ms;
	}

	info->last_accessed_lba = end_lba;
	return 0;
}

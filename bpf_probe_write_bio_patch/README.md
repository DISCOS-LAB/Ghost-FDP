# BPF helper 212: probe_write_kernel_bio_write_stream

**Kernel base:** Linux **6.17.12**. Other versions may need line-offset fixes when applying.

Kernel patch for FDP inference: BPF may set **only** `struct bio::bi_write_stream` (1 byte).

| Helper | Args | Behavior |
|--------|------|----------|
| `bpf_probe_write_kernel_bio_write_stream` (ID **212**) | `(struct bio *bio, u8 stream_id)` | `copy_to_kernel_nofault(&bio->bi_write_stream, &stream_id, sizeof(stream_id))`; `-EINVAL` if `bio` is NULL |

## Modified files

### `include/uapi/linux/bpf.h`

- Register helper **212** in the `__BPF_FUNC_MAPPER` list:
  - `FN(probe_write_kernel_bio_write_stream, 212, ##ctx)`

### `kernel/trace/bpf_trace.c`

- `#include <linux/blk_types.h>` — for `struct bio`
- Implement `BPF_CALL_2(bpf_probe_write_kernel_bio_write_stream, ...)`
- `BTF_ID_LIST(bpf_bio_btf_ids)` / `BTF_ID(struct, bio)` — verifier type for arg1
- Add `bpf_probe_write_kernel_bio_write_stream_proto` (returns integer; GPL-only)
  - `arg1_type = ARG_PTR_TO_BTF_ID` (`struct bio *`)
  - `arg2_type = ARG_ANYTHING` (`u8 stream_id`)
- Wire `BPF_FUNC_probe_write_kernel_bio_write_stream` in `bpf_trace_func_proto()`; same `LOCKDOWN_BPF_WRITE_USER` gate as `bpf_probe_write_user`

## Patch file

`0003-bpf-add-probe_write_kernel-helper.patch` — apply with `patch -p1` from the kernel source root.

**Note:** Helper ID 212 must be free in your tree. If another patch already uses 212, resolve the conflict before applying.

---

## Security / Defensive Perspective

This helper does **not** provide arbitrary kernel writes. It enforces a **three-layer defense** by restricting the target type, the writable field, and the write size.

### 1. Target type restriction (who can write)

- The first argument is **`struct bio *bio`**.
- The prototype sets `arg1_type = ARG_PTR_TO_BTF_ID` with `BTF_ID(struct, bio)`, so the verifier accepts only a pointer the program can prove is `struct bio`.
- An arbitrary kernel address or a different object type is rejected at load time; the helper is not invoked.
- `arg2` is `ARG_ANYTHING` (`u8 stream_id` only). The helper is also omitted from `bpf_trace_func_proto()` when `LOCKDOWN_BPF_WRITE_USER` is set.
- A NULL `bio` returns `-EINVAL` at run time.

### 2. Field whitelist (which field is written)

- The write address is **not** taken from an external argument; the helper uses a **single fixed field** internally.
- It always performs `copy_to_kernel_nofault` only on **`&bio->bi_write_stream`**.
- Other fields of **struct bio** (e.g. bi_iter, bi_opf) cannot be written through this helper.

### 3. Size restriction (how much is written)

- The write size is fixed at **`sizeof(stream_id)`**; `stream_id` is `u8`, so **exactly one byte** is written.
- `bi_write_stream` is a u8, so one byte is correct and no overflow into adjacent fields occurs.

### Summary

| Layer | Restriction |
|-------|-------------|
| Target | `struct bio *` only (`ARG_PTR_TO_BTF_ID`) |
| Field | `bi_write_stream` only (field whitelist) |
| Size | 1 byte fixed (overflow prevention) |

Together, these ensure the operation is a **limited write to the bi_write_stream field of struct bio only**, not a general kernel memory write.

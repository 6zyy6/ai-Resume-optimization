# 九轮检查 · 第 9 轮 · 最终独立复核

STATUS: READY

## Scope

- Range：`3c9e3ac..11d0d53`
- 仅核验上一轮：2 个 Important、1 个 Minor。
- Fresh command：`.venv/bin/python -m pytest packages/api/tests/test_acceptance_release_review.py packages/api/tests/test_acceptance_evidence_security.py packages/api/tests/test_task4_round8_release_gates.py -q`
- Fresh result：`119 passed in 0.97s`。

## Finding disposition

### I1：真实 FD 竞态测试

`ADDRESSED`

- 测试不再 monkeypatch 未使用的 `Path.read_*`，而是包装生产模块实际使用的 `os.open/os.read`（`packages/api/tests/test_acceptance_release_review.py:178-219`）。
- wrapper 记录目标文件 FD；第一次对该 FD 执行 `os.read` 前，真实执行：
  1. `os.replace(original_path, moved_path)`；
  2. 在原路径创建指向 release 目录外文件的 symlink；
  3. 从已打开 FD 继续读取。
- raw command log 和 acceptance item evidence 两类路径均运行此交换，并明确断言 `state["swapped"] is True`、原路径已是 symlink、移走的原文件 hash 等于 manifest、外部替代文件 hash 不等（`test_acceptance_release_review.py:222-261`）。
- manifest 用例同样在 manifest FD 打开后交换路径，外部 manifest 携带不同 commit；验证器仍读取已打开的原 manifest 并成功，随后断言原路径已指向外部文件（`test_acceptance_release_review.py:264-288`）。
- 生产实现保持 dir FD 逐组件 `O_NOFOLLOW`，文件打开后在同一个 FD 上 `fstat → os.read → fstat`；路径替换不会改变已打开对象（`packages/api/scripts/verify_acceptance_evidence.py:295-339`）。

结论：竞态确实发生，测试不再因预置 hash mismatch 或 invalid JSON 假绿；生产同 FD 读取行为得到有效证据。

### I2：candidate commit 外部绑定

`ADDRESSED`

- 函数签名强制要求 `expected_commit_sha`，省略参数会由 Python 直接拒绝（`verify_acceptance_evidence.py:78`）。
- expected 与 manifest SHA 均必须是 40 位小写 hex；随后用 `hmac.compare_digest` 比较，不一致 fail closed（`verify_acceptance_evidence.py:78-80,97-111,366-367`）。
- CLI 的 `--expected-commit-sha` 为 `required=True`（`verify_acceptance_evidence.py:379-384`）；独立 `--help` 检查确认该参数为必填 option。
- mismatch 测试传入格式合法的另一个 40 位 SHA，要求稳定 `ValueError` 且错误指向 candidate commit（`test_acceptance_release_review.py:20-34`）。

结论：manifest 不能再仅凭自报格式合法的 commit 通过；候选版本由调用方提供并强制匹配。

### M1：规格与生产 146 IDs 漂移

`ADDRESSED`

- 生产集合公开为唯一 `ACCEPTANCE_IDS`（`verify_acceptance_evidence.py:67-72`），实际仍为 146 个唯一 ID。
- 测试直接解析权威 `11-acceptance-and-evidence.md` 的表格 ID，断言总数 146、唯一数 146、集合与生产常量完全相等（`test_acceptance_release_review.py:37-51`）。
- 因此规格增删、改号或出现非连续 ID 时，不再可能仅靠生产与 fixture 的相同 group count 双绿。

结论：当前权威文档与生产验证器完全一致，未来漂移会触发测试失败。

## Findings

- Critical：0
- Important：0
- Minor：0

## Ready verdict

`READY`

本次复核范围内的 2 个 Important 和 1 个 Minor 均有真实实现与 fresh 自动化证据，未发现新阻断问题。

## Residual release blockers

真实 PostgreSQL、Web、小程序、Worker/Pi、Task 7 Export、生产部署和真实 146 项发布证据仍属于后续交付范围；不计入本 diff findings，也不因本次 `READY` 被视为已经完成。

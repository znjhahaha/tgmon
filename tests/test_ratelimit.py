"""访客限流器（RateLimiter）单测。"""
from __future__ import annotations

from tgmon.admin.deps import RateLimiter


def test_allows_under_limit():
    rl = RateLimiter(limit_per_min=5)
    now = 1000.0
    for _ in range(5):
        assert rl.check("k", now=now) is True


def test_blocks_over_limit():
    rl = RateLimiter(limit_per_min=3)
    now = 2000.0
    for _ in range(3):
        assert rl.check("k", now=now) is True
    assert rl.check("k", now=now) is False
    assert rl.check("k", now=now) is False


def test_recovers_after_window():
    rl = RateLimiter(limit_per_min=2, window=60.0)
    now = 3000.0
    assert rl.check("k", now=now) is True
    assert rl.check("k", now=now) is True
    assert rl.check("k", now=now) is False
    # 窗口滑出最早的记录后恢复
    assert rl.check("k", now=now + 61.0) is True


def test_keys_are_independent():
    rl = RateLimiter(limit_per_min=1)
    now = 4000.0
    assert rl.check("guest-a:1.2.3.4", now=now) is True
    assert rl.check("guest-a:1.2.3.4", now=now) is False
    # 另一个 key（不同用户或不同 IP）不受影响
    assert rl.check("guest-b:5.6.7.8", now=now) is True


def test_sliding_window_counts_only_recent():
    """滑动窗口：窗口内过期的记录不计入配额。"""
    rl = RateLimiter(limit_per_min=2, window=60.0)
    assert rl.check("k", now=0.0) is True
    # t=0 的记录在 t=59 时仍在窗口内；t=61 已滑出
    assert rl.check("k", now=59.0) is True
    assert rl.check("k", now=59.5) is False
    assert rl.check("k", now=61.0) is True


def test_prunes_stale_buckets():
    """桶数量过多时清理过期 key，防内存无限涨。"""
    rl = RateLimiter(limit_per_min=1)
    now = 5000.0
    for i in range(20000):
        rl.check(f"k-{i}", now=now)
    # 塞满后触发清理：过期桶被回收，新 key 依旧可用
    assert rl.check("fresh", now=now + 120.0) is True
    assert len(rl._hits) < 20000

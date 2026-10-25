using System;

internal static class QuotaViewTests
{
    static void Assert(bool condition, string name) { if (!condition) throw new Exception(name); }
    static int Main()
    {
        DateTimeOffset now = DateTimeOffset.FromUnixTimeSeconds(1792940000);
        Assert(QuotaView.Remaining(null, true) == "—", "missing percentage is unknown");
        Assert(QuotaView.Remaining(0, true) == "0%", "actual exhaustion is zero");
        Assert(QuotaView.Remaining(100, true) == "100%", "actual full quota");
        Assert(QuotaView.Remaining(33.25, true) == "33.3%", "percentage precision");
        Assert(QuotaView.Remaining(33, false) == "—", "stale percentage is hidden");
        Assert(QuotaView.Countdown(now.ToUnixTimeSeconds()+3661, now, true) == "01:01:01 后重置", "countdown hours");
        Assert(QuotaView.Countdown(now.ToUnixTimeSeconds()+90061, now, true) == "1天 01:01:01 后重置", "weekly countdown days");
        Assert(QuotaView.Countdown(now.ToUnixTimeSeconds(), now, true) == "等待额度刷新", "no fake reset");
        Assert(QuotaView.Countdown(null, now, true) == "重置时间未知", "missing reset");
        Assert(!QuotaView.IsFresh(now.AddSeconds(-91), now), "stale after 90 seconds");
        Assert(!QuotaView.IsFresh(now.AddMinutes(1), now), "future cache is not current");
        Assert(!QuotaView.IsCurrent(now, now.ToUnixTimeSeconds()-1, now), "elapsed reset hides old percentage");
        Assert(QuotaView.Label(15, true) == "15 分钟额度", "actual duration not assumed five hours");
        Assert(QuotaView.Label(10080, false) == "每周额度", "weekly label");
        Console.WriteLine("Quota view: 14 checks passed.");
        return 0;
    }
}

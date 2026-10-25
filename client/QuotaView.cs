using System;
using System.Globalization;

internal static class QuotaView
{
    internal static bool IsFresh(DateTimeOffset? fetched, DateTimeOffset now)
    {
        if (!fetched.HasValue) return false;
        double age = (now - fetched.Value).TotalSeconds;
        return age >= -5 && age <= 90;
    }
    internal static bool IsCurrent(DateTimeOffset? fetched, long? reset, DateTimeOffset now)
    {
        if (!IsFresh(fetched, now)) return false;
        return !reset.HasValue || (reset.Value > 0 && reset.Value > now.ToUnixTimeSeconds());
    }
    internal static string Remaining(double? value, bool current)
    {
        if (!current || !value.HasValue || Double.IsNaN(value.Value) || Double.IsInfinity(value.Value) || value.Value < 0 || value.Value > 100) return "—";
        return value.Value.ToString("0.#", CultureInfo.InvariantCulture) + "%";
    }
    internal static string Countdown(long? reset, DateTimeOffset now, bool fresh)
    {
        if (!fresh) return "额度需刷新";
        if (!reset.HasValue || reset.Value <= 0) return "重置时间未知";
        long seconds = (long)Math.Ceiling(reset.Value - (now - new DateTimeOffset(1970, 1, 1, 0, 0, 0, TimeSpan.Zero)).TotalSeconds);
        if (seconds <= 0) return "等待额度刷新";
        TimeSpan span = TimeSpan.FromSeconds(seconds);
        string clock = String.Format("{0:00}:{1:00}:{2:00}", span.Hours, span.Minutes, span.Seconds);
        return (span.Days > 0 ? span.Days + "天 " : "") + clock + " 后重置";
    }
    internal static string Label(double? minutes, bool primary)
    {
        if (!minutes.HasValue || minutes <= 0) return primary ? "主要额度" : "第二额度";
        if (minutes == 300) return "5 小时额度";
        if (minutes == 10080) return "每周额度";
        if (minutes.Value % 1440 == 0) return (minutes.Value / 1440).ToString("0", CultureInfo.InvariantCulture) + " 天额度";
        if (minutes.Value % 60 == 0) return (minutes.Value / 60).ToString("0", CultureInfo.InvariantCulture) + " 小时额度";
        return minutes.Value.ToString("0.#", CultureInfo.InvariantCulture) + " 分钟额度";
    }
}

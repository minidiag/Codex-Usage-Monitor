using System;
using System.Collections.Generic;
using System.Globalization;
using System.Numerics;

internal sealed class CycleDisplay
{
    internal string Tokens="—";
    internal string Hint="当期 Token 统计正在同步；仅统计当前 Windows 用户。";
    internal bool Fresh;
    internal bool Ended;
}

internal static class CycleView
{
    static Dictionary<string,object> Map(object value) {return value as Dictionary<string,object> ?? new Dictionary<string,object>();}
    static object Get(Dictionary<string,object> data,string key) {object value;return data.TryGetValue(key,out value)?value:null;}
    static string Text(object value) {return value==null?"":Convert.ToString(value,CultureInfo.InvariantCulture);}
    static string Number(object value) {BigInteger n;return BigInteger.TryParse(Text(value),out n) && n>=0?n.ToString("N0",CultureInfo.InvariantCulture):"—";}

    internal static CycleDisplay Read(object data,string account,string principal,string bucket,long? reset,double? duration,DateTimeOffset now)
    {
        var display=new CycleDisplay();
        if(account=="" || principal=="" || !reset.HasValue || !duration.HasValue)return display;
        try
        {
            var response=Map(Get(Map(Get(Map(data),"accounts")),account));
            if(Text(Get(response,"account_key"))!=account || Text(Get(response,"principal_key"))!=principal)return display;
            DateTimeOffset fetched;
            if(!DateTimeOffset.TryParse(Text(Get(response,"as_of")),CultureInfo.InvariantCulture,DateTimeStyles.None,out fetched))return display;
            Dictionary<string,object> exact=null,nearby=null;
            int nearbyCount=0;
            foreach(object item in Get(response,"cycles") as object[] ?? new object[0])
            {
                var cycle=Map(item);long ends;double mins;
                if(Text(Get(cycle,"bucket"))!=bucket || !Int64.TryParse(Text(Get(cycle,"resets_at")),out ends) || ends<=0 || ends>253402300799L
                    || !Double.TryParse(Text(Get(cycle,"duration_mins")),NumberStyles.Float,CultureInfo.InvariantCulture,out mins) || mins!=duration.Value)continue;
                DateTimeOffset start,end;
                if(!DateTimeOffset.TryParse(Text(Get(cycle,"starts_at")),out start) || !DateTimeOffset.TryParse(Text(Get(cycle,"ends_at")),out end))continue;
                if(ends==reset.Value)exact=cycle;
                // The live API can round the same reset boundary one second either way.
                // Only an unambiguous, currently active server cycle may bridge that difference.
                if(Math.Abs(ends-reset.Value)<=1 && start<=now && now<end)
                {
                    if(ends==reset.Value){exact=cycle;nearby=cycle;nearbyCount=1;break;}
                    nearby=cycle;nearbyCount++;
                }
            }
            var selected=nearbyCount==1?nearby:exact;
            if(selected==null)return display;
            DateTimeOffset selectedStart=DateTimeOffset.Parse(Text(Get(selected,"starts_at"))),selectedEnd=DateTimeOffset.Parse(Text(Get(selected,"ends_at")));
            display.Ended=now>=selectedEnd;
            if(display.Ended){display.Hint="该周期已结束，等待新的官方额度周期；历史累计已保存在服务器。";return display;}
            if(now<selectedStart)return display;
            display.Fresh=(now-fetched).TotalSeconds>=-5 && (now-fetched).TotalSeconds<=35;
            var totals=Map(Get(selected,"totals"));display.Tokens=Number(Get(totals,"total"));
            display.Hint=(display.Fresh?"当期已采集 Token：":"当期缓存 Token：")+display.Tokens+"\n仅当前 Windows 用户和所选账号，部分覆盖。\n北京时间 "+
                selectedStart.ToOffset(TimeSpan.FromHours(8)).ToString("MM-dd HH:mm:ss")+" 至 "+selectedEnd.ToOffset(TimeSpan.FromHours(8)).ToString("MM-dd HH:mm:ss")+
                "\n输入 "+Number(Get(totals,"input"))+" · 缓存输入 "+Number(Get(totals,"cached_input"))+"\n输出 "+Number(Get(totals,"output"))+" · 推理输出 "+Number(Get(totals,"reasoning_output"))+
                "\n周期起点按官方重置时间和周期长度推定；统计记录保存在服务器。\n查询更新 "+fetched.ToOffset(TimeSpan.FromHours(8)).ToString("HH:mm:ss");
            if(Text(Get(selected,"resets_at"))!=reset.Value.ToString(CultureInfo.InvariantCulture))display.Hint+="\n官方重置时间有 1 秒差异，显示服务器当前周期累计。";
        }
        catch {return new CycleDisplay();}
        return display;
    }
}

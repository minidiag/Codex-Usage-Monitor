using System;
using System.Collections.Generic;

internal static class CycleViewTests
{
    static int count;
    static void Check(bool value,string label){if(!value)throw new Exception(label);count++;}
    [STAThread]
    static void Main()
    {
        var now=DateTimeOffset.Parse("2026-10-26T00:00:00Z");long reset=now.AddHours(3).ToUnixTimeSeconds();
        string account=new string('a',64),principal=new string('b',64);
        var totals=new Dictionary<string,object>{{"total","0"}};
        var cycle=new Dictionary<string,object>{{"bucket","primary"},{"duration_mins","300"},{"resets_at",reset.ToString()},
            {"starts_at",now.AddHours(-2).ToString("o")},{"ends_at",now.AddHours(3).ToString("o")},{"totals",totals}};
        var response=new Dictionary<string,object>{{"account_key",account},{"principal_key",principal},{"as_of",now.ToString("o")},{"cycles",new object[]{cycle}}};
        var data=new Dictionary<string,object>{{"accounts",new Dictionary<string,object>{{account,response}}}};
        var value=CycleView.Read(data,account,principal,"primary",reset,300,now);
        Check(value.Tokens=="0" && value.Fresh,"real zero is a valid fresh total");
        totals["total"]="123456789012345678901234567890";
        Check(CycleView.Read(data,account,principal,"primary",reset,300,now).Tokens=="123,456,789,012,345,678,901,234,567,890","large integers remain exact");
        Check(CycleView.Read(data,account,new string('c',64),"primary",reset,300,now).Tokens=="—","other Windows user hidden");
        Check(CycleView.Read(data,new string('d',64),principal,"primary",reset,300,now).Tokens=="—","other account hidden");
        Check(CycleView.Read(data,account,principal,"secondary",reset,300,now).Tokens=="—","other bucket hidden");
        Check(CycleView.Read(data,account,principal,"primary",reset+1,300,now).Tokens!="—","one second boundary rounding uses the unique active server period");
        Check(CycleView.Read(data,account,principal,"primary",reset+2,300,now).Tokens=="—","other reset period not reused");
        Check(CycleView.Read(data,account,principal,"primary",reset,301,now).Tokens=="—","duration mismatch hidden");
        response["as_of"]=now.AddSeconds(-40).ToString("o");value=CycleView.Read(data,account,principal,"primary",reset,300,now);
        Check(!value.Fresh && value.Tokens!="—" && value.Hint.Contains("缓存"),"old data identified as cache");
        response["as_of"]=now.ToString("o");cycle["ends_at"]=now.ToString("o");value=CycleView.Read(data,account,principal,"primary",reset,300,now);
        Check(value.Ended && value.Tokens=="—","early reset hides previous period counter");
        var active=new Dictionary<string,object>(cycle);
        active["resets_at"]=(reset+1).ToString();active["ends_at"]=now.AddHours(3).AddSeconds(1).ToString("o");
        response["cycles"]=new object[]{cycle,active};
        value=CycleView.Read(data,account,principal,"primary",reset,300,now);
        Check(!value.Ended && value.Tokens!="—" && value.Hint.Contains("1 秒"),"ended exact boundary cannot hide the active one-second neighbor");
        active["resets_at"]=(reset+2).ToString();
        Check(CycleView.Read(data,account,principal,"primary",reset,300,now).Ended,"actual early reset is not replaced by a different period");
        active["resets_at"]=(reset+1).ToString();
        var conflicting=new Dictionary<string,object>(active);conflicting["resets_at"]=(reset-1).ToString();
        response["cycles"]=new object[]{cycle,active,conflicting};
        Check(CycleView.Read(data,account,principal,"primary",reset,300,now).Ended,"ambiguous nearby cycles never guessed");
        Check(CycleView.Read(null,account,principal,"primary",reset,300,now).Tokens=="—","missing data is not zero");
        Check(CycleView.Read(data,account,principal,"primary",null,300,now).Tokens=="—","unknown reset is not guessed");
        cycle["ends_at"]=now.AddHours(3).ToString("o");
        var slot=new Dictionary<string,object>{{"status","available"},{"cycle",cycle}};
        response["buckets"]=new Dictionary<string,object>{{"primary",slot}};
        response["cycles"]=new object[0];
        Check(CycleView.Read(data,account,principal,"primary",reset,300,now).Tokens!="—","current slot does not need history records");
        foreach(string state in new[]{"boundary_pending","boundary_review","observation_stale","waiting_new_cycle"})
        {
            slot["status"]=state;
            value=CycleView.Read(data,account,principal,"primary",reset,300,now);
            Check(value.Tokens=="—" && value.StatusText!="同步中","boundary state is explicit and never a fake zero");
        }
        Console.WriteLine("Cycle view: "+count+" checks passed.");
    }
}

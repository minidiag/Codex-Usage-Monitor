using System;
using System.Collections.Generic;
using System.Web.Script.Serialization;

internal static class AccountViewTests
{
    static int count;
    static void Check(bool value,string message) {if(!value)throw new Exception(message);count++;}
    [STAThread]
    static void Main()
    {
        string key=new string('a',64),other=new string('b',64);
        var data=new Dictionary<string,object> {
            {"identity",new Dictionary<string,object>{{"accounts",new object[]{key}}}},
            {"local_account_keys",new object[]{key}},
            {"catalog",new Dictionary<string,object>{{"accounts",new object[]{other}}}},
            {"local_profiles",new object[]{new Dictionary<string,object>{{"account_key",key},{"display_name","明文姓名"},{"avatar_path","https://example.test/remote-avatar.png"}}}}
        };
        var choices=AccountView.Choices(data);
        Check(choices.Count==3,"one local account plus all/unassigned");
        Check(choices[0].DisplayName=="明文姓名","show actual local name");
        Check(choices[0].Key==key,"anonymous key stays internal");
        Check(choices[0].Initials=="明","name initial fallback");
        Check(!choices[0].HasAvatar,"remote image paths never reach WPF");
        Check(choices[1].DisplayName=="全部账号","aggregate label is clear");
        Check(choices[2].Key=="unassigned","unknown selection remains available");
        foreach(var choice in choices) Check(!choice.DisplayName.Contains(key) && !choice.DisplayName.Contains(other),"no anonymous identifiers shown");
        data["local_profiles"]=new object[0];
        Check(AccountView.Choices(data)[0].DisplayName=="账号名称暂不可用","missing name is not replaced with a hash");
        data["local_account_keys"]=new object[]{key,other};
        data["local_profiles"]=new object[]{new Dictionary<string,object>{{"account_key",key},{"display_name","账户甲"}},new Dictionary<string,object>{{"account_key",other},{"display_name","账户乙"}}};
        choices=AccountView.Choices(data);
        Check(choices.Count==4,"multiple local accounts remain separately selectable");
        Check(choices.Exists(a=>a.Key==key && a.DisplayName=="账户甲"),"first clear account mapping");
        Check(choices.Exists(a=>a.Key==other && a.DisplayName=="账户乙"),"second clear account mapping");
        Check(choices[0].Key!=choices[1].Key,"distinct account selection keys");
        Console.WriteLine("Account view: "+count+" checks passed.");
    }
}

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Web.Script.Serialization;

internal sealed class AccountChoice
{
    public string Key {get;set;}
    public string DisplayName {get;set;}
    public string Initials {get;set;}
    public ImageSource Avatar {get;set;}
    public bool HasAvatar {get {return Avatar!=null;}}
}

internal static class AccountView
{
    static bool updating;
    static string signature;
    static string requested;
    static string avatarRoot;
    static JavaScriptSerializer json=new JavaScriptSerializer();
    static Dictionary<string,object> Map(object value) {return value as Dictionary<string,object> ?? new Dictionary<string,object>();}
    static object Get(Dictionary<string,object> data,string key) {object value;return data.TryGetValue(key,out value)?value:null;}
    static string Text(object value) {return value==null?"":Convert.ToString(value,CultureInfo.InvariantCulture);}
    static ComboBox Combo(Window window) {return (ComboBox)window.FindName("AccountScope");}

    static ImageSource Avatar(object value)
    {
        try
        {
            string path=Path.GetFullPath(Text(value));
            if(avatarRoot==null || !path.StartsWith(avatarRoot+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase)) return null;
            if(!File.Exists(path) || new FileInfo(path).Length>2*1024*1024) return null;
            var image=new BitmapImage();image.BeginInit();image.CacheOption=BitmapCacheOption.OnLoad;image.DecodePixelWidth=48;image.UriSource=new Uri(path,UriKind.Absolute);image.EndInit();image.Freeze();return image;
        }
        catch {return null;}
    }

    internal static List<AccountChoice> Choices(Dictionary<string,object> data)
    {
        var profiles=new Dictionary<string,Dictionary<string,object>>();
        foreach(object value in Get(data,"local_profiles") as object[] ?? new object[0])
        {var profile=Map(value);profiles[Text(Get(profile,"account_key"))]=profile;}
        var keys=new SortedSet<string>();
        foreach(object key in Get(data,"local_account_keys") as object[] ?? new object[0]) keys.Add(Text(key));
        foreach(object key in Get(Map(Get(data,"identity")),"accounts") as object[] ?? new object[0]) keys.Add(Text(key));
        var result=new List<AccountChoice>();
        foreach(string key in keys)
        {
            Dictionary<string,object> profile;
            if(!profiles.TryGetValue(key,out profile)) profile=new Dictionary<string,object>();
            string name=Text(Get(profile,"display_name"));
            if(String.IsNullOrWhiteSpace(name)) name="账号名称暂不可用";
            string initial=StringInfo.GetNextTextElement(name).ToUpperInvariant();
            result.Add(new AccountChoice {Key=key,DisplayName=name,Initials=initial,Avatar=Avatar(Get(profile,"avatar_path"))});
        }
        result.Sort(delegate(AccountChoice a,AccountChoice b){return String.Compare(a.DisplayName,b.DisplayName,StringComparison.CurrentCulture);});
        result.Add(new AccountChoice {Key="",DisplayName="全部账号",Initials="∑"});
        result.Add(new AccountChoice {Key="unassigned",DisplayName="未归属用量",Initials="?"});
        return result;
    }

    internal static bool Refresh(Window window,Dictionary<string,object> data)
    {
        string selected=Text(Get(Map(Get(data,"filters")),"account"));
        if(requested!=null)
        {
            if(requested!=selected)return false;
            requested=null;
        }
        string next=json.Serialize(new object[]{Get(data,"local_profiles"),Get(data,"local_account_keys"),Get(data,"identity"),selected});
        if(signature==next)return true;
        var combo=Combo(window);
        if(combo.IsDropDownOpen)return true; // Do not dismiss a menu while its profile cache refreshes.
        updating=true;
        try
        {
            var choices=Choices(data);combo.ItemsSource=choices;
            foreach(var choice in choices) if(choice.Key==selected){combo.SelectedItem=choice;break;}
            signature=next;
        }
        finally {updating=false;}
        return true;
    }

    internal static void Attach(Window window,Action<string,object> write,Action waiting,string state)
    {
        avatarRoot=Path.GetFullPath(Path.Combine(state,"avatars"));
        ((TextBlock)window.FindName("WindowsScopeCaption")).Text="当前 Windows 用户 · "+Environment.UserName;
        Combo(window).SelectionChanged+=delegate
        {
            if(updating)return;
            var selected=Combo(window).SelectedItem as AccountChoice;
            if(selected==null)return;
            requested=selected.Key;
            // Only account selection is a local preference; the agent enforces the Windows principal.
            write("filters",new Dictionary<string,object>{{"account",selected.Key}});waiting();
        };
    }
}

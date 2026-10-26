using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;

internal static class ProxySettingsWindow
{
    static Window dialog;
    static ComboBox mode;
    static TextBox address;
    static TextBlock status;
    static Button save,test;
    static string state,root;
    static JavaScriptSerializer json=new JavaScriptSerializer();
    static TextBlock Label(string text){return new TextBlock {Text=text,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,5,0,5)};}
    static Button Button(string text){return new Button {Content=text,Padding=new Thickness(14,6,14,6),Margin=new Thickness(0,0,10,0),Foreground=Brushes.Black};}
    static void Result(string message,bool ok)
    {status.Text=message;status.Foreground=ok?new SolidColorBrush(Color.FromRgb(102,221,181)):new SolidColorBrush(Color.FromRgb(240,154,151));}

    internal static bool TrySettings(int selected,string value,out Dictionary<string,object> settings)
    {
        settings=null;
        if(selected==1 || selected==2){settings=new Dictionary<string,object>{{"mode",selected==1?"direct":"environment"}};return true;}
        Uri parsed;value=(value??"").Trim();
        if(selected!=0 || !Uri.TryCreate(value,UriKind.Absolute,out parsed) || (parsed.Scheme!="http" && parsed.Scheme!="https")
            || String.IsNullOrEmpty(parsed.Host) || parsed.UserInfo!="" || parsed.Query!="" || parsed.Fragment!=""
            || (parsed.AbsolutePath!="" && parsed.AbsolutePath!="/") || parsed.Port<=0 || parsed.Port>65535)return false;
        foreach(char character in value)if(Char.IsWhiteSpace(character))return false;
        settings=new Dictionary<string,object>{{"proxy",value}};return true;
    }
    static bool ReadForm(out Dictionary<string,object> settings)
    {
        if(TrySettings(mode.SelectedIndex,address.Text,out settings))return true;
        Result("请填写 HTTP/HTTPS 代理地址，例如 http://127.0.0.1:7890；地址不能带用户名、密码或路径。",false);return false;
    }
    static void Save()
    {
        Dictionary<string,object> settings;if(!ReadForm(out settings))return;
        string path=Path.Combine(state,"quota-network.json"),temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
        try
        {
            File.WriteAllText(temporary,json.Serialize(settings),new UTF8Encoding(false));
            if(File.Exists(path))File.Replace(temporary,path,null);else File.Move(temporary,path);
            Result("已保存。采集器将在下一轮检查中使用新设置，无需重启。",true);
        }
        catch {Result("保存失败，请检查当前用户状态目录的访问权限。",false);}
        finally {try{if(File.Exists(temporary))File.Delete(temporary);}catch {}}
    }
    static string Python()
    {
        var runtime=json.DeserializeObject(File.ReadAllText(Path.Combine(state,"runtime.json"),Encoding.UTF8)) as Dictionary<string,object>;
        object value;if(runtime==null || !runtime.TryGetValue("pythonw",out value))throw new IOException();
        string executable=Path.Combine(Path.GetDirectoryName(Convert.ToString(value)),"python.exe");
        if(!File.Exists(executable))throw new IOException();return executable;
    }
    static void Test()
    {
        Dictionary<string,object> settings;if(!ReadForm(out settings))return;
        string executable;
        try{executable=Python();}catch {Result("未找到客户端 Python 运行环境，无法测试连接。",false);return;}
        string payload=json.Serialize(settings);Window target=dialog;
        mode.IsEnabled=false;address.IsEnabled=false;test.IsEnabled=false;save.IsEnabled=false;
        status.Text="正在测试当前填写的设置，尚未保存…";status.Foreground=Brushes.White;
        ThreadPool.QueueUserWorkItem(delegate
        {
            bool ok=false;string message="连接测试失败，请检查运行环境后重试。";
            try
            {
                var info=new ProcessStartInfo(executable,"-m client.quota_settings --test") {WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true};
                info.EnvironmentVariables["PYTHONUTF8"]="1";
                using(var process=Process.Start(info))
                {
                    process.StandardInput.Write(payload);process.StandardInput.Close();
                    if(!process.WaitForExit(25000)){process.Kill();process.WaitForExit();message="连接测试超时，请检查代理或网络。";}
                    else
                    {
                        string output=process.StandardOutput.ReadToEnd();
                        var result=new JavaScriptSerializer().DeserializeObject(output) as Dictionary<string,object>;object flag,text;
                        if(result!=null && result.TryGetValue("ok",out flag) && result.TryGetValue("message",out text))
                        {ok=flag is bool && (bool)flag;message=Convert.ToString(text);}
                    }
                }
            }
            catch { }
            if(target.Dispatcher.HasShutdownStarted)return;
            try {target.Dispatcher.BeginInvoke(new Action(delegate
            {
                if(dialog!=target)return;
                mode.IsEnabled=true;address.IsEnabled=mode.SelectedIndex==0;test.IsEnabled=true;save.IsEnabled=true;
                Result(message+"（连接测试不会保存设置）",ok);
            }));}catch(InvalidOperationException) { }
        });
    }
    internal static void Show(Window owner,string projectRoot,string directory)
    {
        if(dialog!=null){dialog.Activate();return;}
        root=projectRoot;state=directory;
        dialog=new Window {Title="额度代理设置",Width=590,SizeToContent=SizeToContent.Height,ResizeMode=ResizeMode.NoResize,Owner=owner,WindowStartupLocation=WindowStartupLocation.CenterScreen,Topmost=owner.Topmost,Background=new SolidColorBrush(Color.FromRgb(20,31,49)),Foreground=Brushes.White,FontSize=13};
        var panel=new StackPanel {Margin=new Thickness(22)};
        panel.Children.Add(new TextBlock {Text="额度代理设置",FontSize=19,FontWeight=FontWeights.SemiBold,Margin=new Thickness(0,0,0,8)});
        panel.Children.Add(Label("用于读取 Codex 账户额度。阿里云数据上传保持 HTTPS 直连。"));
        panel.Children.Add(Label("连接方式"));
        mode=new ComboBox {Margin=new Thickness(0,2,0,8),Foreground=Brushes.Black};
        mode.Items.Add("使用指定代理");mode.Items.Add("直接连接（绕过代理）");mode.Items.Add("继承采集器启动环境");mode.SelectedIndex=2;
        AutomationProperties.SetName(mode,"额度连接方式");panel.Children.Add(mode);
        panel.Children.Add(Label("代理地址（HTTP/HTTPS）"));
        address=new TextBox {Padding=new Thickness(8,6,8,6),Background=new SolidColorBrush(Color.FromRgb(34,51,76)),Foreground=Brushes.White,BorderBrush=new SolidColorBrush(Color.FromRgb(65,83,110)),Margin=new Thickness(0,2,0,4)};
        AutomationProperties.SetName(address,"额度代理地址");panel.Children.Add(address);
        panel.Children.Add(Label("例如 http://127.0.0.1:7890。支持不带用户名和密码的 HTTP/HTTPS 代理。"));
        panel.Children.Add(Label("“继承启动环境”使用采集器启动时的代理环境变量；更改这些环境变量后需重启客户端。"));
        status=Label("保存后自动生效；只检测连接不会改变已保存的设置。");AutomationProperties.SetName(status,"代理设置状态");panel.Children.Add(status);
        string loadError=null;
        try
        {
            string path=Path.Combine(state,"quota-network.json");
            if(File.Exists(path))
            {
                var existing=json.DeserializeObject(File.ReadAllText(path,Encoding.UTF8)) as Dictionary<string,object>;object value;
                if(existing!=null && existing.TryGetValue("proxy",out value)){address.Text=Convert.ToString(value);mode.SelectedIndex=0;}
                else if(existing!=null && existing.TryGetValue("mode",out value) && Convert.ToString(value)=="direct")mode.SelectedIndex=1;
                else if(existing==null || !existing.TryGetValue("mode",out value) || Convert.ToString(value)!="environment")loadError="现有设置无效，请修正后保存。";
            }
        }
        catch {loadError="现有设置无法读取，请检查地址后重新保存。";}
        address.IsEnabled=mode.SelectedIndex==0;mode.SelectionChanged+=delegate {address.IsEnabled=mode.SelectedIndex==0;};
        var buttons=new StackPanel {Orientation=Orientation.Horizontal,HorizontalAlignment=HorizontalAlignment.Right,Margin=new Thickness(0,16,0,0)};
        test=Button("检测连接");test.Click+=delegate {Test();};buttons.Children.Add(test);
        save=Button("保存");save.Click+=delegate {Save();};buttons.Children.Add(save);
        var close=Button("关闭");close.Click+=delegate {dialog.Close();};buttons.Children.Add(close);panel.Children.Add(buttons);
        dialog.Content=panel;dialog.Closed+=delegate {dialog=null;};if(loadError!=null)Result(loadError,false);dialog.Show();
    }
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

internal static class DashboardAccessWindow
{
    static Window dialog;
    internal static void Show(Window owner,string root,string state)
    {
        if(dialog!=null){dialog.Activate();return;}
        var target=new Window {Title="网页仪表盘登录",Width=440,SizeToContent=SizeToContent.Height,ResizeMode=ResizeMode.NoResize,Owner=owner,WindowStartupLocation=WindowStartupLocation.CenterScreen,Topmost=owner.Topmost,Background=new SolidColorBrush(Color.FromRgb(20,31,49)),Foreground=Brushes.White,FontSize=13};
        dialog=target;
        var panel=new StackPanel {Margin=new Thickness(22)};
        panel.Children.Add(new TextBlock {Text="获取当前 Windows 用户的只读凭据",FontSize=17,Margin=new Thickness(0,0,0,14)});
        panel.Children.Add(new TextBlock {Text="复制后粘贴到仪表盘的“只读凭据”框即可登录。程序会先校验读取范围，管理员级凭据不能通过此入口复制。",TextWrapping=TextWrapping.Wrap});
        var status=new TextBlock {Text="点击下方按钮获取。凭据不会显示在窗口或写入状态文件。",TextWrapping=TextWrapping.Wrap,Foreground=Brushes.LightGray,Margin=new Thickness(0,14,0,18)};
        panel.Children.Add(status);
        var buttons=new WrapPanel();
        var copy=new Button {Content="复制用户只读凭据",Padding=new Thickness(12,7,12,7),Margin=new Thickness(0,0,10,0)};
        var open=new Button {Content="打开仪表盘",Padding=new Thickness(12,7,12,7),IsEnabled=false};
        buttons.Children.Add(copy);buttons.Children.Add(open);panel.Children.Add(buttons);target.Content=panel;
        string dashboard=null;
        open.Click+=delegate {try {Process.Start(new ProcessStartInfo(dashboard){UseShellExecute=true});}catch {status.Text="无法打开浏览器，请手动访问服务器仪表盘。";}};
        copy.Click+=delegate
        {
            copy.IsEnabled=false;status.Text="正在验证当前用户读取权限…";
            ThreadPool.QueueUserWorkItem(delegate
            {
                string token=null,url=null,code="access_unavailable";
                try
                {
                    var json=new JavaScriptSerializer();
                    var runtime=json.DeserializeObject(File.ReadAllText(Path.Combine(state,"runtime.json"),Encoding.UTF8)) as Dictionary<string,object>;
                    string python=Path.Combine(Path.GetDirectoryName(Convert.ToString(runtime["pythonw"])),"python.exe");
                    using(var process=Process.Start(new ProcessStartInfo(python,"-m client.dashboard_access") {WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=Encoding.UTF8,WindowStyle=ProcessWindowStyle.Hidden}))
                    {
                        if(!process.WaitForExit(15000)){process.Kill();process.WaitForExit();}
                        else if(process.ExitCode==0)
                        {
                            var value=json.DeserializeObject(process.StandardOutput.ReadToEnd()) as Dictionary<string,object>;
                            if(value!=null && value.ContainsKey("ok") && value["ok"] is bool && (bool)value["ok"])
                            {
                                string candidate=Convert.ToString(value["read_token"]);Uri parsed;
                                if(Regex.IsMatch(candidate,@"\A[A-Za-z0-9_-]{32,256}\z") && Uri.TryCreate(Convert.ToString(value["dashboard_url"]),UriKind.Absolute,out parsed) && parsed.Scheme=="https")
                                {token=candidate;url=parsed.AbsoluteUri;}
                            }
                            else if(value!=null && value.ContainsKey("code"))code=Convert.ToString(value["code"]);
                        }
                    }
                }
                catch { }
                if(target.Dispatcher.HasShutdownStarted)return;
                target.Dispatcher.BeginInvoke(new Action(delegate
                {
                    if(dialog!=target){token=null;return;}
                    copy.IsEnabled=true;
                    if(token==null){status.Text=code=="user_scope_required"?"当前凭据不是此 Windows 用户的专属只读凭据，请联系管理员配置。":"暂时无法获取，请检查网络、采集器配置及服务状态后重试。";return;}
                    try {Clipboard.SetText(token);dashboard=url;open.IsEnabled=true;status.Text="已复制用户级只读凭据。请打开仪表盘，粘贴并连接。";}
                    catch {status.Text="剪贴板暂时不可用，请再点一次复制。";}
                    finally {token=null;}
                }));
            });
        };
        target.Closed+=delegate {dashboard=null;dialog=null;};target.Show();
    }
}

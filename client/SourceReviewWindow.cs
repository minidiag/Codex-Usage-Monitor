using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

internal static class SourceReviewWindow
{
    static Window dialog;
    static StackPanel body;
    static TextBlock note;
    static string state,pending,lastReport;
    static DateTimeOffset pendingAt;
    static DispatcherTimer timer;
    static JavaScriptSerializer json=new JavaScriptSerializer {MaxJsonLength=8*1024*1024};
    static Dictionary<string,object> Map(object value){return value as Dictionary<string,object> ?? new Dictionary<string,object>();}
    static object Get(Dictionary<string,object> data,string key){object value;return data.TryGetValue(key,out value)?value:null;}
    static string Text(object value){return value==null?"":Convert.ToString(value,CultureInfo.InvariantCulture);}
    static bool Flag(object value){return value is bool && (bool)value;}
    static object[] Rows(object value){return value as object[] ?? new object[0];}
    static TextBlock Label(string value,double size=12){return new TextBlock {Text=value,FontSize=size,TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,3,0,3)};}
    static string LocalTime(object value)
    {DateTimeOffset parsed;return DateTimeOffset.TryParse(Text(value),out parsed)?parsed.ToOffset(TimeSpan.FromHours(8)).ToString("yyyy-MM-dd HH:mm:ss"):"未知";}
    static void Request(string action,Dictionary<string,object> fields=null)
    {
        try
        {
        var request=fields ?? new Dictionary<string,object>();pending=Guid.NewGuid().ToString();pendingAt=DateTimeOffset.UtcNow;request["id"]=pending;request["action"]=action;
        string target=Path.Combine(state,"source-review-request.json"),temporary=target+".ui.tmp";
        File.WriteAllText(temporary,json.Serialize(request),new UTF8Encoding(false));
        if(File.Exists(target))File.Replace(temporary,target,null);else File.Move(temporary,target);
        note.Text="正在核实日志和服务器基线，请稍候…";body.IsEnabled=false;
        }
        catch {pending=null;body.IsEnabled=true;note.Text="无法保存核实请求，请检查本机状态目录权限后重试。";}
    }
    static void Refresh()
    {
        if(pending!=null && (DateTimeOffset.UtcNow-pendingAt).TotalSeconds>40)note.Text="尚未收到核实结果。请确认采集器正在运行；服务器连接异常时可重新核实。";
        try
        {
            string raw=File.ReadAllText(Path.Combine(state,"source-review.json"),Encoding.UTF8);
            var report=Map(json.DeserializeObject(raw));
            if(pending!=null && Text(Get(report,"request_id"))!=pending)return;
            if(raw==lastReport)return;
            pending=null;lastReport=raw;body.IsEnabled=true;body.Children.Clear();
            note.Text=Text(Get(Map(Get(report,"result")),"message"))+"\n核实时间："+LocalTime(Get(report,"generated_at"));
            var streams=Rows(Get(report,"streams"));
            if(streams.Length==0)body.Children.Add(Label("没有需要核实的暂停来源。"));
            int number=0;
            foreach(object value in streams)
            {
                var stream=Map(value);var group=new StackPanel {Margin=new Thickness(0,8,0,12)};
                group.Children.Add(Label("暂停来源 "+(++number)+" · "+Text(Get(stream,"reason"))+" · 待传 "+Text(Get(stream,"pending"))+" 条",15));
                group.Children.Add(Label("原基线："+LocalTime(Get(stream,"baseline_time"))+" · "+Text(Get(stream,"baseline_model"))));
                group.Children.Add(Label(Text(Get(Map(Get(stream,"server")),"message"))));
                var sources=Rows(Get(stream,"sources"));
                foreach(object sourceValue in sources)
                {
                    var source=Map(sourceValue);var panel=new StackPanel {Margin=new Thickness(12,5,12,7)};
                    panel.Children.Add(new TextBox {Text=Text(Get(source,"path")),IsReadOnly=true,TextWrapping=TextWrapping.Wrap,BorderThickness=new Thickness(0),Background=Brushes.Transparent,Foreground=Brushes.White,Margin=new Thickness(0,3,0,3)});
                    panel.Children.Add(Label("检查点 "+Text(Get(source,"points"))+" · 原基线"+(Flag(Get(source,"anchor_matches"))?"匹配":"缺失或不同")+" · 历史记录"+(Flag(Get(source,"records_match"))?"一致":"不一致")+(Flag(Get(source,"excluded"))?" · 当前已排除":"")));
                    panel.Children.Add(Label("时间范围："+LocalTime(Get(source,"first_time"))+" 至 "+LocalTime(Get(source,"last_time"))));
                    if(Rows(Get(source,"warnings")).Length>0)panel.Children.Add(Label("此文件仍有解析或覆盖限制；恢复来源后不代表完整历史已经导入。"));
                    if(Get(source,"common_points")!=null)panel.Children.Add(Label("与基线匹配来源共同检查点："+Text(Get(source,"common_points"))+" · "+(Flag(Get(source,"aligned_prefix"))?"可按相同顺序对齐":"不能证明是同序列副本")));
                    foreach(object issue in Rows(Get(source,"issues")))panel.Children.Add(Label("• "+Text(issue)));
                    if(Flag(Get(source,"eligible")))panel.Children.Add(Label("原基线及已有记录均可验证。选择后仅继续此文件，其他已登记文件会被排除。"));
                    var choose=new Button {Content="选择此来源并恢复",IsEnabled=Flag(Get(source,"eligible")),HorizontalAlignment=HorizontalAlignment.Left,Padding=new Thickness(12,5,12,5),Margin=new Thickness(0,5,0,2)};
                    var fields=new Dictionary<string,object>{{"stream_id",Get(stream,"stream_id")},{"source_id",Get(source,"source_id")},{"fingerprint",Get(source,"fingerprint")},{"revision",Get(report,"revision")}};
                    string selectedPath=Text(Get(source,"path"));int excluded=Math.Max(0,sources.Length-1);
                    choose.Click+=delegate
                    {
                        if(MessageBox.Show(dialog,"将以这份日志继续采集：\n"+selectedPath+"\n\n其他 "+excluded+" 份已登记日志将从该来源采集中排除，其用量不会合并。操作前保存本地账本备份并记录选择；原日志和已保存事件保持保留。\n\n确认选择这份来源？","确认来源选择",MessageBoxButton.YesNo,MessageBoxImage.Question)==MessageBoxResult.Yes)
                            Request("resume",new Dictionary<string,object>(fields));
                    };
                    panel.Children.Add(choose);group.Children.Add(new Border {Child=panel,BorderBrush=new SolidColorBrush(Color.FromRgb(65,83,110)),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(5),Margin=new Thickness(0,5,0,5)});
                }
                body.Children.Add(group);
            }
            var audit=Rows(Get(report,"audit"));
            if(audit.Length>0)
            {
                body.Children.Add(Label("最近的恢复记录",15));
                foreach(object entry in audit){var row=Map(entry);body.Children.Add(Label(LocalTime(Get(row,"created_at"))+" · "+Text(Get(row,"selected_path"))+"\n备份："+Text(Get(row,"backup"))));}
            }
        }
        catch(IOException) { }
        catch {note.Text="核实报告暂不可读，请重新核实。";}
    }
    internal static void Show(Window owner,string directory)
    {
        if(dialog!=null){dialog.Activate();return;}
        state=directory;pending=null;lastReport=null;
        dialog=new Window {Title="采集来源核实与恢复",Width=930,Height=690,MinWidth=650,MinHeight=400,Owner=owner,WindowStartupLocation=WindowStartupLocation.CenterScreen,Background=new SolidColorBrush(Color.FromRgb(20,31,49)),Foreground=Brushes.White,Topmost=owner.Topmost};
        var layout=new DockPanel {Margin=new Thickness(18)};
        var header=new StackPanel();DockPanel.SetDock(header,Dock.Top);
        header.Children.Add(Label("核实本地日志，选择能够复现原基线和历史记录的来源",18));
        header.Children.Add(Label("同会话的不同日志不会自动合并。文件变化、历史冲突或服务器无法核实时会拒绝恢复。报告和路径仅保存在本机。"));
        var buttons=new StackPanel {Orientation=Orientation.Horizontal,Margin=new Thickness(0,8,0,8)};
        var inspect=new Button {Content="重新核实",Padding=new Thickness(12,6,12,6),Margin=new Thickness(0,0,10,0)};
        inspect.Click+=delegate {Request("inspect");};buttons.Children.Add(inspect);
        var auto=new Button {Content="恢复可证明的重复副本",Padding=new Thickness(12,6,12,6)};
        auto.Click+=delegate {Request("auto");};buttons.Children.Add(auto);header.Children.Add(buttons);
        note=Label("正在请求采集器核实…");header.Children.Add(note);layout.Children.Add(header);
        body=new StackPanel();layout.Children.Add(new ScrollViewer {Content=body,VerticalScrollBarVisibility=ScrollBarVisibility.Auto});
        dialog.Content=layout;timer=new DispatcherTimer {Interval=TimeSpan.FromSeconds(1)};timer.Tick+=delegate {Refresh();};
        dialog.Closed+=delegate {timer.Stop();dialog=null;};Request("inspect");timer.Start();dialog.Show();
    }
}

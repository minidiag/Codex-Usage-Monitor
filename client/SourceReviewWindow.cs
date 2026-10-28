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
    static void CloseAlert(Dictionary<string,object> alert,object revision)
    {
        var prompt=new Window {Title="关闭此来源告警",Owner=dialog,Width=580,SizeToContent=SizeToContent.Height,ResizeMode=ResizeMode.NoResize,WindowStartupLocation=WindowStartupLocation.CenterOwner,Topmost=dialog.Topmost,Background=new SolidColorBrush(Color.FromRgb(20,31,49)),Foreground=Brushes.White,UseLayoutRounding=true,SnapsToDevicePixels=true};
        var panel=new StackPanel {Margin=new Thickness(22)};
        panel.Children.Add(Label("关闭此来源当前问题的重复告警",17));
        foreach(object path in Rows(Get(alert,"paths")))panel.Children.Add(Label(Text(path)));
        panel.Children.Add(Label("历史数据及待传记录保留，原采集暂停状态保留；关闭不代表缺失用量已恢复。文件重新出现、内容或来源状态变化时会重新告警。"));
        panel.Children.Add(Label("关闭原因（必填，最多 200 字）"));
        var reason=new TextBox {MaxLength=200,Margin=new Thickness(0,5,0,8),Padding=new Thickness(8)};panel.Children.Add(reason);
        var error=Label("");panel.Children.Add(error);
        var buttons=new StackPanel {Orientation=Orientation.Horizontal,HorizontalAlignment=HorizontalAlignment.Right};
        var cancel=new Button {Content="取消",Padding=new Thickness(14,6,14,6),Margin=new Thickness(0,0,12,0)};cancel.Click+=delegate {prompt.Close();};buttons.Children.Add(cancel);
        var confirm=new Button {Content="确认关闭告警",Padding=new Thickness(14,6,14,6)};
        confirm.Click+=delegate
        {
            if(String.IsNullOrWhiteSpace(reason.Text)){error.Text="请填写关闭原因，例如：日志已删除且无法找回。";return;}
            var fields=new Dictionary<string,object>{{"stream_id",Get(alert,"stream_id")},{"fingerprint",Get(alert,"fingerprint")},{"revision",revision},{"reason",reason.Text.Trim()}};
            prompt.Close();Request("close_alert",fields);
        };
        buttons.Children.Add(confirm);panel.Children.Add(buttons);prompt.Content=panel;prompt.ShowDialog();
    }
    static void Alerts(Dictionary<string,object> report)
    {
        var health=Map(Get(report,"source_health"));
        var active=Rows(Get(health,"active_alerts"));var closed=Rows(Get(health,"closed_alerts"));
        body.Children.Add(Label("来源告警 · 待处理 "+active.Length+" · 已关闭 "+closed.Length,17));
        body.Children.Add(Label("关闭只确认当前覆盖缺口，不会删除数据或解除采集暂停。其他来源继续采集；来源发生变化会重新告警。"));
        foreach(object value in active)
        {
            var row=Map(value);var panel=new StackPanel {Margin=new Thickness(12)};
            panel.Children.Add(Label(Text(Get(row,"message")),14));
            foreach(object path in Rows(Get(row,"paths")))panel.Children.Add(new TextBox {Text=Text(path),IsReadOnly=true,TextWrapping=TextWrapping.Wrap,Background=Brushes.Transparent,Foreground=Brushes.White,BorderThickness=new Thickness(0)});
            panel.Children.Add(Label("保留 "+Text(Get(row,"retained_records"))+" 条记录 · 待传 "+Text(Get(row,"pending"))+" 条\n最后已采集时间："+LocalTime(Get(row,"last_record_at"))));
            var close=new Button {Content="关闭此来源告警…",HorizontalAlignment=HorizontalAlignment.Left,Padding=new Thickness(12,5,12,5),Margin=new Thickness(0,6,0,0)};
            var alert=row;object revision=Get(report,"revision");close.Click+=delegate {CloseAlert(alert,revision);};panel.Children.Add(close);
            body.Children.Add(new Border {Child=panel,BorderBrush=new SolidColorBrush(Color.FromRgb(96,74,52)),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(5),Margin=new Thickness(0,5,0,5)});
        }
        if(closed.Length>0)body.Children.Add(Label("已关闭的告警 / 已确认覆盖缺口",15));
        foreach(object value in closed)
        {
            var row=Map(value);body.Children.Add(Label(LocalTime(Get(row,"closed_at"))+" · 原因："+Text(Get(row,"reason"))));
            foreach(object path in Rows(Get(row,"paths")))body.Children.Add(Label(Text(path)));
            var reopen=new Button {Content="重新打开此告警",HorizontalAlignment=HorizontalAlignment.Left,Padding=new Thickness(12,5,12,5),Margin=new Thickness(0,3,0,8)};
            var fields=new Dictionary<string,object>{{"stream_id",Get(row,"stream_id")},{"fingerprint",Get(row,"fingerprint")},{"closure_id",Get(row,"closure_id")},{"revision",Get(report,"revision")}};
            reopen.Click+=delegate {Request("reopen_alert",new Dictionary<string,object>(fields));};body.Children.Add(reopen);
        }
    }
    static void Tracking(Dictionary<string,object> report)
    {
        var tracking=Map(Get(report,"source_tracking"));
        if(tracking.Count>0)
            body.Children.Add(Label("现存来源增量追踪 · 当前 "+Text(Get(tracking,"existing_files"))+" 个文件 · 本轮变化候选 "+Text(Get(tracking,"content_candidates"))+" 个\n启动清点次数 "+Text(Get(tracking,"initial_censuses"))+" · 本轮目录枚举 "+Text(Get(tracking,"directory_reads"))+" 次。未变化的日志不重读，已消失路径移出活动追踪。"));
        var changes=Rows(Get(report,"source_changes"));
        if(changes.Length==0)return;
        var list=new StackPanel();list.Children.Add(Label("这里只能确认观察到的路径变化，无法可靠判断由谁、因为什么删除。历史数据和覆盖缺口仍保留。"));
        foreach(object value in changes)
        {
            var row=Map(value);string kind=Text(Get(row,"kind"));
            string label=kind=="renamed_observed"?"观察到迁移/改名":kind=="compressed_observed"?"发现同名压缩文件":kind=="removed_observed"?"观察到路径消失":kind=="not_present_at_startup"?"启动时已不存在":"发现新增来源";
            list.Children.Add(Label(LocalTime(Get(row,"observed_at"))+" · "+label+"\n"+Text(Get(row,"path"))+"\n"+Text(Get(row,"message"))+(Get(row,"related_path")!=null?"\n关联路径："+Text(Get(row,"related_path")):"")));
        }
        body.Children.Add(new Expander {Header="来源文件变化记录（最近 "+changes.Length+" 项）",Content=list,Margin=new Thickness(0,10,0,10)});
    }
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
            Alerts(report);
            Tracking(report);
            var streams=Rows(Get(report,"streams"));
            if(streams.Length==0)body.Children.Add(Label("没有暂停来源；压缩与缺失来源的检查结果见下方。"));
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
            var health=Map(Get(report,"source_health"));
            var archives=Rows(Get(health,"archives"));
            var missing=Rows(Get(health,"missing"));
            var unknown=Rows(Get(health,"unregistered_archives"));
            if(archives.Length+missing.Length+unknown.Length>0)
            {
                body.Children.Add(Label("压缩与缺失来源",16));
                body.Children.Add(Label("核实只读取日志并缓存结果，不新增或覆盖用量事件。已保存的待传记录继续重试；原文件或压缩文件变化时会重新检查。"));
                foreach(object value in archives)
                {
                    var row=Map(value);string status=Text(Get(row,"status"));
                    body.Children.Add(Label(status=="synced"?"已同步历史来源":status=="pending"?"已核实 · 等待上传确认":status=="no_usage"?"无可采集用量的历史来源":"压缩来源待核实",14));
                    body.Children.Add(new TextBox {Text=Text(Get(row,"path")),IsReadOnly=true,TextWrapping=TextWrapping.Wrap,Background=Brushes.Transparent,Foreground=Brushes.White,BorderThickness=new Thickness(0)});
                    body.Children.Add(Label("原文件："+Text(Get(row,"original_path"))+"\n"+Text(Get(row,"message"))+"\n待传 "+Text(Get(row,"pending"))+" 条"));
                    if(status=="unverified")body.Children.Add(Label("处理方式：确认文件写入完成后点“重新核实”；仍失败时恢复原 .jsonl 日志。缺少解码组件需先安装 zstandard。不会把未知记录自动计入统计。"));
                }
                foreach(object value in missing)
                {
                    var row=Map(value);body.Children.Add(Label("文件缺失："+Text(Get(row,"path"))+"\n未找到可用副本，请恢复原日志或检查来源目录，然后重新核实。"));
                }
                foreach(object value in unknown)
                {
                    var row=Map(value);body.Children.Add(Label(Text(Get(row,"path"))+"\n"+Text(Get(row,"message"))));
                }
            }
            var audit=Rows(Get(report,"audit"));
            if(audit.Length>0)
            {
                body.Children.Add(Label("最近的恢复记录",15));
                foreach(object entry in audit){var row=Map(entry);body.Children.Add(Label(LocalTime(Get(row,"created_at"))+" · "+Text(Get(row,"selected_path"))+"\n备份："+Text(Get(row,"backup"))));}
            }
            var alertAudit=Rows(Get(report,"alert_audit"));
            if(alertAudit.Length>0)
            {
                body.Children.Add(Label("最近的告警操作记录",15));
                foreach(object value in alertAudit)
                {
                    var row=Map(value);string action=Text(Get(row,"action"));
                    string label=action=="close"?"关闭告警":action=="reopen"?"手动重新打开":action=="changed"?"来源变化，重新告警":action=="retired"?"移出活动追踪，缺口保留":"原问题已消失";
                    body.Children.Add(Label(LocalTime(Get(row,"created_at"))+" · "+label+"\n"+Text(Get(row,"reason"))+Text(Get(row,"message"))+(Get(row,"backup")!=null?"\n备份："+Text(Get(row,"backup")):"")));
                }
            }
        }
        catch(IOException) { }
        catch {note.Text="核实报告暂不可读，请重新核实。";}
    }
    internal static void Show(Window owner,string directory)
    {
        if(dialog!=null){dialog.Activate();return;}
        state=directory;pending=null;lastReport=null;
        dialog=new Window {Title="采集来源核实与恢复",Width=930,Height=690,MinWidth=650,MinHeight=400,Owner=owner,WindowStartupLocation=WindowStartupLocation.CenterScreen,Background=new SolidColorBrush(Color.FromRgb(20,31,49)),Foreground=Brushes.White,Topmost=owner.Topmost,UseLayoutRounding=true,SnapsToDevicePixels=true};
        var layout=new DockPanel {Margin=new Thickness(18)};
        var header=new StackPanel();DockPanel.SetDock(header,Dock.Top);
        header.Children.Add(Label("核实本地日志、压缩历史与缺失来源",18));
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

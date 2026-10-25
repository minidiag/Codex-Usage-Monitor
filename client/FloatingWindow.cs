// Framework WPF host: no SDK installation, browser runtime or shell window needed.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using Path = System.IO.Path;

internal static class FloatingWindowApp
{
    static string root = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".."));
    static string state = UserState();
    static JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 1024 * 1024 };
    static Window window;
    static DispatcherTimer timer;
    static MenuItem pauseItem;
    static Dictionary<string, object> last;
    static string activeSignature;
    static string UserState()
    {
        string key;
        using(var sha=SHA256.Create()) key=BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-","").Substring(0,16);
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"CodexUsageMonitor",key,"state");
    }
    static void WriteControl(string key,object value)
    {
        Dictionary<string,object> control;
        try {control=Read("control.json");} catch {control=new Dictionary<string,object>();}
        control[key]=value;
        string target=Path.Combine(state,"control.json"), temporary=target+".ui.tmp";
        File.WriteAllText(temporary,json.Serialize(control),new UTF8Encoding(false));
        if(File.Exists(target)) File.Replace(temporary,target,null); else File.Move(temporary,target);
    }
    static void AccountPending()
    {
        foreach(string suffix in new[]{"5h","Today","Week","Month"}) SetText("Value"+suffix,"—");
        Widget<StackPanel>("ActiveList").Children.Clear();activeSignature=null;
        SetText("ActiveCount","正在切换…");SetText("ActiveEmpty","正在读取所选账号…");Widget<TextBlock>("ActiveEmpty").Visibility=Visibility.Visible;
        foreach(string suffix in new[]{"Primary","Secondary"})
        {SetText("Quota"+suffix+"Remaining","—");SetText("Quota"+suffix+"Reset","正在切换账号…");Widget<ProgressBar>("Quota"+suffix+"Bar").Value=0;}
        SetText("QuotaNote","正在读取所选账号额度…");SetText("StatusText","正在切换账号…");
    }
    static Dictionary<string, object> Map(object value) { return value as Dictionary<string, object> ?? new Dictionary<string, object>(); }
    static object Get(Dictionary<string, object> data, string key) { object value; return data.TryGetValue(key, out value) ? value : null; }
    static string Text(object value) { return value == null ? "" : Convert.ToString(value, CultureInfo.InvariantCulture); }
    static bool Bool(object value) { return value != null && Convert.ToBoolean(value, CultureInfo.InvariantCulture); }
    static T Widget<T>(string name) where T : class { return window.FindName(name) as T; }
    static void SetText(string name, string text) { Widget<TextBlock>(name).Text = text; }
    static Dictionary<string, object> Read(string name) { return Map(json.DeserializeObject(File.ReadAllText(Path.Combine(state, name), Encoding.UTF8))); }
    static string Number(object value) { BigInteger number; return BigInteger.TryParse(Text(value), out number) && number >= 0 ? number.ToString("N0", CultureInfo.InvariantCulture) : "—"; }
    static DateTimeOffset Date(object value) { return DateTimeOffset.Parse(Text(value), CultureInfo.InvariantCulture, DateTimeStyles.None); }
    static double? Numeric(object value) { double parsed; return value != null && Double.TryParse(Text(value), NumberStyles.Float, CultureInfo.InvariantCulture, out parsed) && !Double.IsNaN(parsed) && !Double.IsInfinity(parsed) ? (double?)parsed : null; }
    static long? Seconds(object value) { long parsed; return value != null && Int64.TryParse(Text(value), out parsed) && parsed > 0 && parsed <= 253402300799L ? (long?)parsed : null; }

    static void RefreshQuota(Dictionary<string, object> data)
    {
        try
        {
            var quota = Map(Get(data, "quota"));
            string selected = Text(Get(Map(Get(data,"filters")),"account"));
            var loggedIn=new HashSet<string>();
            foreach(object value in Get(Map(Get(data,"identity")),"accounts") as object[] ?? new object[0]) loggedIn.Add(Text(value));
            if(Text(Get(quota,"account_key"))!="" && !loggedIn.Contains(Text(Get(quota,"account_key")))) quota=new Dictionary<string,object>();
            if(selected!="")
            {
                quota=new Dictionary<string,object>();
                foreach(object item in Get(data,"quotas") as object[] ?? new object[0])
                {var value=Map(item);if(Text(Get(value,"account_key"))==selected && loggedIn.Contains(selected)){quota=value;break;}}
            }
            DateTimeOffset now = DateTimeOffset.UtcNow;
            DateTimeOffset parsed;
            DateTimeOffset? fetched = DateTimeOffset.TryParse(Text(Get(quota, "fetched_at")), CultureInfo.InvariantCulture, DateTimeStyles.None, out parsed) ? (DateTimeOffset?)parsed : null;
            bool fresh = QuotaView.IsFresh(fetched, now);
            var rows = Get(quota, "windows") as object[] ?? new object[0];
            foreach (string key in new[] { "primary", "secondary" })
            {
                var value = new Dictionary<string, object>();
                foreach (object item in rows) { var row = Map(item); if (Text(Get(row, "key")) == key) { value = row; break; } }
                string suffix = key == "primary" ? "Primary" : "Secondary";
                long? reset = Seconds(Get(value, "resets_at"));
                double? remaining = Numeric(Get(value, "remaining_percent"));
                bool current = QuotaView.IsCurrent(fetched, reset, now) && value.Count > 0;
                string principal=Text(Get(Map(Get(Map(Get(data,"identity")),"subject")),"principal_key"));
                string account=Text(Get(quota,"account_key"));
                var cycle=CycleView.Read(Get(data,"quota_cycle_stats"),account,principal,key,reset,Numeric(Get(value,"duration_mins")),now);
                if(cycle.Ended)current=false;
                SetText("Quota" + suffix + "Label", QuotaView.Label(Numeric(Get(value, "duration_mins")), key == "primary")+"（"+cycle.Tokens+"）");
                Widget<TextBlock>("Quota"+suffix+"Label").ToolTip=cycle.Hint;
                Widget<TextBlock>("Quota"+suffix+"Label").Opacity=cycle.Fresh?1:0.65;
                SetText("Quota" + suffix + "Remaining", QuotaView.Remaining(remaining, current));
                SetText("Quota" + suffix + "Reset", cycle.Ended ? "等待额度刷新" : (value.Count == 0 ? "额度暂不可用" : QuotaView.Countdown(reset, now, fresh)));
                var bar = Widget<ProgressBar>("Quota" + suffix + "Bar");
                bar.Value = current && remaining.HasValue ? Math.Max(0, Math.Min(100, remaining.Value)) : 0;
                bar.Opacity = current ? 1 : 0.35;
                string color = !remaining.HasValue || remaining > 25 ? "#66DDB5" : (remaining > 10 ? "#E8BF73" : "#F09A97");
                bar.Foreground = (Brush)new BrushConverter().ConvertFromString(color);
                Widget<TextBlock>("Quota" + suffix + "Remaining").Foreground = bar.Foreground;
                string hint = reset.HasValue ? "北京时间 " + DateTimeOffset.FromUnixTimeSeconds(reset.Value).ToOffset(TimeSpan.FromHours(8)).ToString("MM-dd HH:mm:ss") + " 重置" : "未提供重置时间";
                if (remaining.HasValue) hint += "\n上次读取的剩余额度 " + remaining.Value.ToString("0.#", CultureInfo.InvariantCulture) + "%";
                Widget<TextBlock>("Quota" + suffix + "Reset").ToolTip = hint;
            }
            string error = Text(Get(quota, "error"));
            string note = "正在读取账户额度…";
            if (fetched.HasValue) note = fresh ? (error == "" ? "Codex 账户 · " : "缓存额度 · ") + fetched.Value.ToOffset(TimeSpan.FromHours(8)).ToString("HH:mm:ss") + " 更新" : "额度已过期，正在刷新";
            else if (error != "") note = error == "codex_not_found" ? "未找到 Codex，额度暂不可用" : "额度读取失败，稍后重试";
            SetText("QuotaNote", note);
            if(selected=="") SetText("QuotaNote","本机当前登录 · "+note);
            else if(quota.Count==0) SetText("QuotaNote","所选账号暂无本机额度数据");
            var cycleStatus=Map(Get(data,"quota_cycle_stats"));
            if(Convert.ToInt32(Get(cycleStatus,"blocked_reports")??0)>0) Widget<TextBlock>("QuotaNote").Text+=" · 部分周期待核对";
            else if(Text(Get(cycleStatus,"error"))!="") Widget<TextBlock>("QuotaNote").Text+=" · 周期用量待同步";
        }
        catch { SetText("QuotaNote", "额度暂不可用"); }
    }

    static void RefreshActiveSessions(Dictionary<string, object> data)
    {
        try
        {
            var view = Map(Get(data, "active_sessions"));
            var rows = Get(view, "sessions") as object[] ?? new object[0];
            DateTimeOffset updated;
            bool fresh = DateTimeOffset.TryParse(Text(Get(view, "updated_at")), CultureInfo.InvariantCulture, DateTimeStyles.None, out updated)
                && (DateTimeOffset.UtcNow - updated).TotalSeconds >= -5 && (DateTimeOffset.UtcNow - updated).TotalSeconds < 35
                && Bool(Get(view, "available")) && Bool(Get(data, "running")) && !Bool(Get(data, "paused"));
            string error = Text(Get(view, "error"));
            bool segmented=Text(Get(Map(Get(data,"filters")),"account"))!="";
            SetText("ActiveCount", fresh ? rows.Length + (error == "" ? " 个 · 已采集" : " 个 · 部分可读") : "需刷新");
            Widget<Border>("ActiveBorder").Opacity = fresh ? 1 : 0.6;
            Widget<Border>("ActiveBorder").ToolTip = "只显示当前 Windows 用户已确认归属的会话分段，随账号选择筛选；不显示整段跨用户历史。列表不会额外加进周期总量。\n" +
                (error != "" ? "部分会话或索引暂不可用。" : "按任务开始、完成或中止事件识别；无明确事件时采用最近 10 分钟日志活动。") +
                "\n每个数据目录检查最近 12 个未归档会话，约每 5 秒刷新。";
            SetText("ActiveEmpty", !Bool(Get(view, "available")) ? "会话索引暂不可用" : (fresh ? (error == "" ? "暂无活跃会话" : "部分会话暂不可读") : "等待会话状态刷新"));
            if(rows.Length==0 && Text(Get(view,"scope_note"))!="") SetText("ActiveEmpty",Text(Get(view,"scope_note")));
            Widget<TextBlock>("ActiveEmpty").Visibility = rows.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
            string signature = json.Serialize(rows);
            if (signature == activeSignature) return;
            activeSignature = signature;
            var panel = Widget<StackPanel>("ActiveList");
            var scroll = Widget<ScrollViewer>("ActiveScroll");
            double offset = scroll.VerticalOffset;
            panel.Children.Clear();
            foreach (object value in rows)
            {
                var row = Map(value);
                var grid = new Grid { Margin = new Thickness(0, 3, 0, 5) };
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(114) });
                var details = new StackPanel { Margin = new Thickness(0, 0, 6, 0), VerticalAlignment = VerticalAlignment.Center };
                string title = Text(Get(row, "title"));
                details.Children.Add(new TextBlock { Text = title, ToolTip = title, FontSize = 11, TextTrimming = TextTrimming.CharacterEllipsis });
                string model = Text(Get(row, "model")) + " " + Text(Get(row, "effort"));
                if (Text(Get(row, "activity_source")) == "recent_log") model += " · 近期活动";
                details.Children.Add(new TextBlock { Text = model.Trim(), ToolTip = model.Trim(), FontSize = 9, Foreground = (Brush)new BrushConverter().ConvertFromString("#7D91B3"), TextTrimming = TextTrimming.CharacterEllipsis });
                grid.Children.Add(details);
                var number = new TextBlock { Text = Number(Get(row, "tokens")), FontFamily = new FontFamily("Segoe UI"), FontSize = 17, FontWeight = FontWeights.SemiBold, Foreground = (Brush)new BrushConverter().ConvertFromString("#66DDB5") };
                var box = new Viewbox { Child = number, Stretch = Stretch.Uniform, StretchDirection = StretchDirection.DownOnly, Width = 110, Height = 28, HorizontalAlignment = HorizontalAlignment.Right,
                    ToolTip = "当前 Windows 用户已采集分段 Token：" + number.Text + "\n按所选账号筛选；会话内累计，不额外计入周期总量。" };
                Grid.SetColumn(box, 1); grid.Children.Add(box);
                panel.Children.Add(grid);
            }
            scroll.ScrollToVerticalOffset(offset);
        }
        catch { SetText("ActiveCount", "会话状态暂不可用"); Widget<Border>("ActiveBorder").Opacity = 0.6; }
    }

    static void StartCollector()
    {
        string python=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","Python","Python312","pythonw.exe");
        try { string configured=Text(Get(Read("runtime.json"),"pythonw")); if(File.Exists(configured)) python=configured; } catch {}
        if(!File.Exists(python)) python=Path.Combine(root,".local","venv","Scripts","pythonw.exe");
        if (!File.Exists(python)) throw new FileNotFoundException("Python runtime missing");
        Process.Start(new ProcessStartInfo(python, "-m client.agent") { WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden });
    }

    static void Refresh()
    {
        if (File.Exists(Path.Combine(state, "stop.request"))) { window.Close(); return; }
        try
        {
            last = Read("status.json");
            string principal=Text(Get(Map(Get(Map(Get(last,"identity")),"subject")),"principal_key"));
            if(Text(Get(last,"view_scope"))!="current_windows_user" || principal=="" || Text(Get(Map(Get(last,"filters")),"principal"))!=principal)
            {AccountPending();SetText("StatusText","正在读取当前 Windows 用户数据…");return;}
            if(!AccountView.Refresh(window,last)) { SetText("StatusText","正在切换账号…"); return; }
            RefreshQuota(last);
            RefreshActiveSessions(last);
            var summary = Map(Get(last, "summary"));
            var periods = Get(summary, "windows") as object[];
            if (periods != null)
            {
                foreach (object item in periods)
                {
                    var period = Map(item); var totals = Map(Get(period, "totals"));
                    string suffix = null;
                    switch (Text(Get(period, "name"))) { case "rolling_5h": suffix = "5h"; break; case "today": suffix = "Today"; break; case "week": suffix = "Week"; break; case "month": suffix = "Month"; break; }
                    if (suffix == null) continue;
                    SetText("Value" + suffix, Number(Get(totals, "total")));
                    Widget<Border>("Card" + suffix).ToolTip = String.Format("输入 {0}\n缓存输入 {1}\n输出 {2}\n推理输出 {3}", Get(totals, "input"), Get(totals, "cached_input"), Get(totals, "output"), Get(totals, "reasoning_output"));
                }
                SetText("UpdatedText", Date(Get(summary, "as_of")).ToOffset(TimeSpan.FromHours(8)).ToString("HH:mm:ss"));
            }
            bool fresh = (DateTimeOffset.UtcNow - Date(Get(last, "updated_at"))).TotalSeconds < 35;
            string message = "已同步 · 部分覆盖", color = "#E8BF73";
            string error = Text(Get(last, "error"));
            bool paused = Bool(Get(last, "paused"));
            if (!Bool(Get(last, "running")) || !fresh) { message = "采集器已停止"; color = "#F09A97"; }
            else if (paused) message = "已暂停采集和同步";
            else if (error != "")
            {
                message = "连接异常 · 正在重试"; color = "#F09A97";
                if (error == "authentication_failed") message = "凭据需要更新";
                else if (error == "queue_full") message = "本地队列已满，采集暂停";
                else if (error == "collector_error" || error == "scan_failed") message = "采集异常，请查看本地状态";
                else if (error == "source_missing") message = "未找到采集来源";
                else if (error == "subject_unavailable") message = "当前 Windows 用户暂不可识别";
            }
            else if (Convert.ToInt64(Get(last, "queued_records") ?? 0) > 0) message = "待上传 " + Text(Get(last, "queued_records")) + " 条";
            else if (Text(Get(summary, "quality")) == "complete") { message = "已同步"; color = "#66DDB5"; }
            if (Bool(Get(last, "running")) && fresh && !paused && error == "" && Convert.ToInt32(Get(last, "blocked_streams") ?? 0) > 0)
            { message = "部分来源暂停，需检查"; color = "#F09A97"; }
            SetText("StatusText", message);
            Widget<Ellipse>("StatusDot").Fill = (Brush)new BrushConverter().ConvertFromString(color);
            pauseItem.Header = paused ? "继续采集和同步" : "暂停采集和同步";
            SetText("CoverageText", "仅当前 Windows 用户 · 部分覆盖");
            Widget<TextBlock>("CoverageText").ToolTip = "按当前 Windows 用户的已确认归属统计；较早未归属到 Windows 用户的历史仍保留在服务器，不混入此窗口。压缩补充和未适配子代理尚未计入。";
            if (Get(last, "started_at") != null) SetText("SinceText", "开始于 " + Date(Get(last, "started_at")).ToOffset(TimeSpan.FromHours(8)).ToString("MM-dd HH:mm") + " · 右键管理");
        }
        catch { SetText("StatusText", "等待采集器启动…"); }
    }

    [STAThread]
    static void Main()
    {
        Directory.CreateDirectory(state);
        string requestedStop = Path.Combine(state, "stop.request");
        if (File.Exists(requestedStop)) File.Delete(requestedStop);
        string key;
        using (var sha = SHA256.Create()) key = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToLowerInvariant()))).Replace("-", "").Substring(0, 16);
        using (var mutex = new Mutex(false, "Local\\CodexUsageMonitorNative-" + key))
        {
            bool owns;
            try { owns = mutex.WaitOne(0); } catch (AbandonedMutexException) { owns = true; }
            if (!owns) { StartCollector(); return; }
            try
            {
                string stop = Path.Combine(state, "stop.request");
                if (File.Exists(stop)) File.Delete(stop);
                StartCollector();
                window = (Window)XamlReader.Parse(File.ReadAllText(Path.Combine(root, "client", "FloatingWindow.xaml"), Encoding.UTF8));
                Rect area = SystemParameters.WorkArea;
                window.Left = area.Right - window.Width - 22; window.Top = area.Bottom - window.Height - 24;
                try
                {
                    var pos = Read("window.json"); double x = Convert.ToDouble(Get(pos, "x")), y = Convert.ToDouble(Get(pos, "y"));
                    if (x >= SystemParameters.VirtualScreenLeft && y >= SystemParameters.VirtualScreenTop && x + window.Width <= SystemParameters.VirtualScreenLeft + SystemParameters.VirtualScreenWidth && y + window.Height <= SystemParameters.VirtualScreenTop + SystemParameters.VirtualScreenHeight - 40) { window.Left = x; window.Top = y; }
                }
                catch { }
                Widget<StackPanel>("DragHandle").MouseLeftButtonDown += delegate(object sender, MouseButtonEventArgs e) { if (e.ButtonState == MouseButtonState.Pressed) window.DragMove(); };
                Widget<Button>("Minimize").Click += delegate { window.WindowState = WindowState.Minimized; };
                Widget<Button>("CloseWindow").Click += delegate { window.Close(); };
                var menu = new ContextMenu();
                pauseItem = new MenuItem { Header = "暂停采集和同步" };
                pauseItem.Click += delegate {WriteControl("paused",!Bool(Get(last ?? new Dictionary<string,object>(),"paused")));};
                var pin = new MenuItem { Header = "窗口置顶", IsCheckable = true, IsChecked = true };
                pin.Click += delegate { window.Topmost = pin.IsChecked; };
                var hide = new MenuItem { Header = "关闭窗口，后台继续采集" }; hide.Click += delegate { window.Close(); };
                var exit = new MenuItem { Header = "停止采集并退出" }; exit.Click += delegate { File.WriteAllText(stop, "stop"); window.Close(); };
                menu.Items.Add(pauseItem); menu.Items.Add(pin); menu.Items.Add(hide); menu.Items.Add(exit);
                Widget<Border>("Surface").ContextMenu = menu;
                AccountView.Attach(window,WriteControl,AccountPending,state);
                timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
                timer.Tick += delegate { Refresh(); };
                window.Closed += delegate { timer.Stop(); File.WriteAllText(Path.Combine(state, "window.json"), json.Serialize(new { x = window.Left, y = window.Top })); };
                Refresh(); timer.Start();
                window.ShowDialog();
            }
            catch (Exception error) { File.WriteAllText(Path.Combine(state, "window.native.log"), DateTime.UtcNow.ToString("o") + " " + error.GetType().Name); }
            finally { if (timer != null) timer.Stop(); mutex.ReleaseMutex(); }
        }
    }
}

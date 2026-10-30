using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

internal enum DockEdge { None, Left, Right, Top, Bottom }

internal static class EdgeGeometry
{
    internal const double SnapDistance = 16, StripSize = 6;

    internal static DockEdge Detect(Rect bounds, Rect area)
    {
        double[] distances = { Math.Abs(bounds.Left-area.Left), Math.Abs(bounds.Right-area.Right),
            Math.Abs(bounds.Top-area.Top), Math.Abs(bounds.Bottom-area.Bottom) };
        int nearest = 0;
        for (int i=1; i<distances.Length; i++) if (distances[i]<distances[nearest]) nearest=i;
        return distances[nearest]<=SnapDistance ? (DockEdge)(nearest+1) : DockEdge.None;
    }

    internal static Rect Fit(Rect bounds, Rect area)
    {
        return new Rect(Math.Max(area.Left,Math.Min(bounds.Left,area.Right-bounds.Width)),
            Math.Max(area.Top,Math.Min(bounds.Top,area.Bottom-bounds.Height)),bounds.Width,bounds.Height);
    }

    internal static Rect Snap(Rect bounds, Rect area, DockEdge edge)
    {
        bounds=Fit(bounds,area);
        if(edge==DockEdge.Left) bounds.X=area.Left;
        if(edge==DockEdge.Right) bounds.X=area.Right-bounds.Width;
        if(edge==DockEdge.Top) bounds.Y=area.Top;
        if(edge==DockEdge.Bottom) bounds.Y=area.Bottom-bounds.Height;
        return bounds;
    }

    internal static Rect Strip(Rect bounds, DockEdge edge)
    {
        if(edge==DockEdge.Left) return new Rect(bounds.Left,bounds.Top,StripSize,bounds.Height);
        if(edge==DockEdge.Right) return new Rect(bounds.Right-StripSize,bounds.Top,StripSize,bounds.Height);
        if(edge==DockEdge.Top) return new Rect(bounds.Left,bounds.Top,bounds.Width,StripSize);
        if(edge==DockEdge.Bottom) return new Rect(bounds.Left,bounds.Bottom-StripSize,bounds.Width,StripSize);
        return bounds;
    }
}

// Collapse the actual window to a visible strip. No content extends onto adjacent monitors.
internal sealed class EdgeAutoHide : IDisposable
{
    [StructLayout(LayoutKind.Sequential)] struct NativePoint { public int X,Y; }
    [StructLayout(LayoutKind.Sequential)] struct NativeRect { public int Left,Top,Right,Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct MonitorInfo
    { public int Size; public NativeRect Monitor,Work; public uint Flags; }
    [DllImport("user32.dll")] static extern IntPtr MonitorFromWindow(IntPtr hwnd,uint flags);
    [DllImport("user32.dll",CharSet=CharSet.Auto)] static extern bool GetMonitorInfo(IntPtr monitor,ref MonitorInfo info);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out NativePoint point);

    readonly Window window;
    readonly UIElement content;
    readonly Border strip;
    readonly DispatcherTimer timer;
    readonly Func<bool> interactionOpen;
    Rect expanded,workArea;
    DockEdge edge;
    bool hidden,dragging,enabled=true;
    DateTime? outsideSince;

    internal EdgeAutoHide(Window window, Func<bool> interactionOpen)
    {
        this.window=window; this.interactionOpen=interactionOpen;
        content=(UIElement)window.Content;
        window.Content=null;
        var host=new Grid();
        strip=new Border { Background=new SolidColorBrush(Color.FromRgb(102,221,181)),
            CornerRadius=new CornerRadius(3), Visibility=Visibility.Collapsed, Cursor=Cursors.Hand,
            ToolTip="移入展开 Codex 用量统计；拖动标题可取消靠边" };
        host.Children.Add(content); host.Children.Add(strip); window.Content=host;
        strip.MouseEnter+=delegate { Reveal(); };
        strip.MouseLeftButtonDown+=delegate { Reveal(); };
        window.SourceInitialized+=delegate { Place(EdgeGeometry.Fit(Bounds(),WorkArea())); };
        window.Activated+=delegate { Reveal(); };
        window.StateChanged+=delegate { if(window.WindowState==WindowState.Normal) Reveal(); };
        timer=new DispatcherTimer { Interval=TimeSpan.FromMilliseconds(100) };
        timer.Tick+=delegate { Tick(); }; timer.Start();
    }

    internal bool Enabled
    {
        get { return enabled; }
        set { enabled=value; outsideSince=null; if(!value) Reveal(); }
    }
    internal Rect SavedBounds { get { return hidden ? expanded : (window.WindowState==WindowState.Normal ? Bounds() : window.RestoreBounds); } }
    internal void BeginDrag() { Reveal(); dragging=true; outsideSince=null; }
    internal void EndDrag()
    {
        dragging=false;
        if(enabled) Snap();
    }
    Rect Bounds() { return new Rect(window.Left,window.Top,window.Width,window.Height); }
    Point FromPixels(int x,int y)
    {
        var source=PresentationSource.FromVisual(window);
        return source==null ? new Point(x,y) : source.CompositionTarget.TransformFromDevice.Transform(new Point(x,y));
    }
    Rect WorkArea()
    {
        var info=new MonitorInfo { Size=Marshal.SizeOf(typeof(MonitorInfo)) };
        if(!GetMonitorInfo(MonitorFromWindow(new WindowInteropHelper(window).Handle,2),ref info)) return SystemParameters.WorkArea;
        return new Rect(FromPixels(info.Work.Left,info.Work.Top),FromPixels(info.Work.Right,info.Work.Bottom));
    }
    void Place(Rect bounds)
    {
        window.Width=bounds.Width; window.Height=bounds.Height;
        window.Left=bounds.Left; window.Top=bounds.Top;
    }
    void Snap()
    {
        workArea=WorkArea(); edge=EdgeGeometry.Detect(Bounds(),workArea);
        if(edge!=DockEdge.None) Place(EdgeGeometry.Snap(Bounds(),workArea,edge));
    }
    void Reveal()
    {
        outsideSince=null;
        if(!hidden) return;
        hidden=false;
        Place(EdgeGeometry.Fit(expanded,WorkArea()));
        strip.Visibility=Visibility.Collapsed; content.Visibility=Visibility.Visible;
    }
    void Tick()
    {
        if(!window.IsVisible || window.WindowState!=WindowState.Normal || dragging) { outsideSince=null; return; }
        if(hidden)
        {
            if(WorkArea()!=workArea) Reveal();
            return;
        }
        if(!enabled || !window.IsEnabled || Mouse.Captured!=null || interactionOpen()) { outsideSince=null; return; }
        foreach(Window owned in window.OwnedWindows) if(owned.IsVisible) { outsideSince=null; return; }
        NativePoint cursor;
        if(!GetCursorPos(out cursor) || Bounds().Contains(FromPixels(cursor.X,cursor.Y))) { outsideSince=null; return; }
        Snap();
        if(edge==DockEdge.None) { outsideSince=null; return; }
        if(!outsideSince.HasValue) { outsideSince=DateTime.UtcNow; return; }
        if((DateTime.UtcNow-outsideSince.Value).TotalMilliseconds<700) return;
        expanded=Bounds(); hidden=true;
        content.Visibility=Visibility.Hidden; strip.Visibility=Visibility.Visible;
        Place(EdgeGeometry.Strip(expanded,edge));
    }
    public void Dispose() { timer.Stop(); }
}

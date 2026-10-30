using System;
using System.Windows;

internal static class EdgeAutoHideTests
{
    static int count;
    static void Check(bool ok,string message) { if(!ok) throw new Exception(message); count++; }
    static void Main()
    {
        // A secondary display left of the primary, with space reserved for its taskbar.
        var area=new Rect(-1920,0,1920,1040);
        var samples=new[] { new Rect(-1910,100,350,666), new Rect(-360,100,350,666),
            new Rect(-1200,10,350,666), new Rect(-1200,364,350,666) };
        for(int i=0;i<samples.Length;i++)
        {
            var edge=(DockEdge)(i+1);
            Check(EdgeGeometry.Detect(samples[i],area)==edge,"detect "+edge);
            var expanded=EdgeGeometry.Snap(samples[i],area,edge);
            var strip=EdgeGeometry.Strip(expanded,edge);
            Check(area.Contains(expanded),"expanded window stays in work area: "+edge);
            Check(area.Contains(strip),"strip stays on its monitor: "+edge);
            Check(expanded.Contains(strip),"strip remains within expanded bounds: "+edge);
            Check((i<2 ? strip.Width : strip.Height)==6,"strip thickness: "+edge);
            Check(EdgeGeometry.Snap(expanded,area,edge)==expanded,"stable snap: "+edge);
        }
        Check(EdgeGeometry.Detect(new Rect(-1200,100,350,666),area)==DockEdge.None,"interior never hides");
        Check(EdgeGeometry.Detect(new Rect(-1903,100,350,666),area)==DockEdge.None,"outside threshold never hides");
        Check(EdgeGeometry.Detect(new Rect(-1904,100,350,666),area)==DockEdge.Left,"threshold inclusive");
        Check(EdgeGeometry.Snap(new Rect(-1200,380,350,666),area,DockEdge.Bottom).Bottom==1040,"bottom avoids taskbar");
        var primary=new Rect(0,0,1920,1040);
        var recovered=EdgeGeometry.Fit(samples[0],primary);
        Check(primary.Contains(recovered),"unplugged monitor position recovers");
        Check(recovered.Size==samples[0].Size,"recovery preserves full expanded size");
        Console.WriteLine("Edge auto-hide: "+count+" checks passed.");
    }
}

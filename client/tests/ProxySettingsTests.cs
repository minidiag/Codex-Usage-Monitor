using System;
using System.Collections.Generic;

internal static class ProxySettingsTests
{
    static int count;
    static void Check(bool value,string message){if(!value)throw new Exception(message);count++;}
    static void Main()
    {
        Dictionary<string,object> settings;
        Check(ProxySettingsWindow.TrySettings(0,"http://127.0.0.1:7890",out settings) && (string)settings["proxy"]=="http://127.0.0.1:7890","custom proxy is retained");
        Check(ProxySettingsWindow.TrySettings(0,"https://[::1]:7890",out settings),"IPv6 HTTPS proxy supported");
        Check(!ProxySettingsWindow.TrySettings(0,"http://user:private@localhost:7890",out settings),"credentials cannot be saved in proxy URLs");
        Check(!ProxySettingsWindow.TrySettings(0,"http://localhost:7890/private",out settings),"endpoint paths are rejected");
        Check(!ProxySettingsWindow.TrySettings(0,"http://localhost:7890/?token=private",out settings),"query credentials are rejected");
        Check(!ProxySettingsWindow.TrySettings(0,"http://localhost:0",out settings),"port zero rejected");
        Check(ProxySettingsWindow.TrySettings(1,"",out settings) && (string)settings["mode"]=="direct","direct mode has an explicit setting");
        Check(ProxySettingsWindow.TrySettings(2,"",out settings) && (string)settings["mode"]=="environment","environment inheritance differs from direct mode");
        Check(!ProxySettingsWindow.TrySettings(9,"http://localhost:7890",out settings),"unknown modes rejected");
        Console.WriteLine("Proxy settings: "+count+" checks passed.");
    }
}

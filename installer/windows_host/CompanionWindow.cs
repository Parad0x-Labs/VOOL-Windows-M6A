using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Forms = System.Windows.Forms;

namespace Vool.WindowsHost;

// A candidate-only desktop surface. The installer continues to launch the existing window.
internal sealed class CompanionWindow : Window
{
    private const int ExStyle = -20;
    private const nint NoActivate = 0x08000000;
    private const nint ToolWindow = 0x00000080;
    private const nint Transparent = 0x00000020;
    private const int MouseActivate = 0x0021;
    private const nint NoActivateResult = 3;
    private const int LeftButton = 0x01;
    private const int NoZOrder = 0x0004;
    private const int NoMove = 0x0002;
    private const int NoSize = 0x0001;
    private const int NoActivateMove = 0x0010;
    private const int FrameChanged = 0x0020;

    private static readonly Uri CompanionUri = new("http://127.0.0.1:11435/companion");
    private readonly WebView2CompositionControl _view = new()
    {
        DefaultBackgroundColor = System.Drawing.Color.Transparent,
    };
    private readonly DispatcherTimer _pointerTimer = new() { Interval = TimeSpan.FromMilliseconds(60) };
    private readonly Action<PetPosition> _positionSettled;
    private readonly Func<BridgeRequest, object> _control;
    private readonly TaskCompletionSource<bool> _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly CoreWebView2Environment _environment;
    private PetRect? _yieldRect;
    private PetPosition? _lastReported;
    private bool _dragging;
    private bool _clickThrough;
    private bool _closed;
    private bool _placed;

    internal bool IsClosed => _closed;

    internal CompanionWindow(CoreWebView2Environment environment, Action<PetPosition> positionSettled,
        Func<BridgeRequest, object> control)
    {
        _environment = environment;
        _positionSettled = positionSettled;
        _control = control;
        Title = "VOOL Companion";
        Width = CompanionGeometry.WindowSize;
        Height = CompanionGeometry.WindowSize;
        WindowStyle = WindowStyle.None;
        AllowsTransparency = true;
        Background = System.Windows.Media.Brushes.Transparent;
        ShowActivated = false;
        ShowInTaskbar = false;
        Topmost = true;
        ResizeMode = ResizeMode.NoResize;
        Content = _view;
        SourceInitialized += (_, _) =>
        {
            var handle = new WindowInteropHelper(this).Handle;
            var required = NoActivate | ToolWindow | Transparent;
            SetWindowLongPtr(handle, ExStyle, GetWindowLongPtr(handle, ExStyle) | required);
            if ((GetWindowLongPtr(handle, ExStyle) & required) != required)
            {
                _ready.TrySetResult(false);
                Dispatcher.BeginInvoke(Close);
                return;
            }
            HwndSource.FromHwnd(handle)?.AddHook(WindowMessage);
            _clickThrough = true;
        };
        Loaded += async (_, _) => await InitializeAsync();
        Closed += (_, _) =>
        {
            _closed = true;
            _pointerTimer.Stop();
            _ready.TrySetResult(false);
            _view.Dispose();
        };
        _pointerTimer.Tick += (_, _) => TrackPointer();
    }

    internal async Task<bool> OpenAsync(CompanionPayload payload)
    {
        if (_closed)
            return false;
        if (!IsVisible)
        {
            Opacity = 0;
            Show();
        }
        bool ready;
        try { ready = await _ready.Task.WaitAsync(TimeSpan.FromSeconds(12)); }
        catch (TimeoutException) { return false; }
        if (!ready || _closed)
            return false;
        if (!_placed)
        {
            Place(payload.DesktopPosition);
            _placed = true;
        }
        if (!await SyncAsync(payload))
            return false;
        Opacity = 1;
        _pointerTimer.Start();
        return true;
    }

    internal async Task<bool> SyncAsync(CompanionPayload payload)
    {
        if (_closed || !_ready.Task.IsCompletedSuccessfully || !_ready.Task.Result)
            return false;
        var script = "Boolean(window.VoolDesktopCompanion"
            + "&&typeof window.VoolDesktopCompanion.update==='function'"
            + "&&(window.VoolDesktopCompanion.update(" + payload.ToRendererJson() + "),true))";
        return await _view.CoreWebView2.ExecuteScriptAsync(script) == "true";
    }

    internal void HideCompanion()
    {
        _pointerTimer.Stop();
        Hide();
    }

    internal void Resume()
    {
        if (_closed || !(_ready.Task.IsCompletedSuccessfully && _ready.Task.Result))
            return;
        Show();
        _pointerTimer.Start();
    }

    internal void SetYieldRect(PetRect? rect)
    {
        _yieldRect = rect;
        TrackPointer();
    }

    internal PetPosition Position()
    {
        var handle = new WindowInteropHelper(this).Handle;
        return GetWindowRect(handle, out var rect)
            ? new PetPosition(rect.Left, rect.Top) : new PetPosition(Left, Top);
    }

    internal PetPosition ResetPosition()
    {
        var primary = Forms.Screen.PrimaryScreen?.Bounds ?? throw new InvalidOperationException("No display");
        GetWindowRect(new WindowInteropHelper(this).Handle, out var bounds);
        var width = bounds.Right - bounds.Left;
        var height = bounds.Bottom - bounds.Top;
        MoveTo(primary.Right - width - 48, primary.Bottom - height - 96);
        ReportPosition();
        return Position();
    }

    internal void BeginDrag()
    {
        if (_closed || !IsVisible || GetAsyncKeyState(LeftButton) >= 0)
            return;
        if (!GetCursorPos(out var cursor)
            || !GetWindowRect(new WindowInteropHelper(this).Handle, out var rect))
            return;
        var frame = new PetRect(rect.Left, rect.Top, rect.Right - rect.Left, rect.Bottom - rect.Top);
        if (!CompanionGeometry.PointHitsCanvas(cursor.X, cursor.Y, frame)
            || !CompanionGeometry.PointHitsPet(cursor.X, cursor.Y, frame, _yieldRect))
            return;
        _dragging = true;
        ReleaseCapture();
        // The companion page requests dragging only from its canvas, never from a control.
        SendMessage(new WindowInteropHelper(this).Handle, 0x00A1, 2, 0);
    }

    private async Task InitializeAsync()
    {
        try
        {
            await _view.EnsureCoreWebView2Async(_environment);
            var core = _view.CoreWebView2;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsWebMessageEnabled = true;
            core.NavigationStarting += (_, e) => e.Cancel = !NativeBridge.TrustedPage(e.Uri, "/companion");
            core.FrameNavigationStarting += (_, e) => e.Cancel = true;
            core.NewWindowRequested += (_, e) => e.Handled = true;
            core.WebMessageReceived += (_, e) =>
            {
                if (!NativeBridge.TryParse(e.Source, core.Source, "/companion", e.WebMessageAsJson,
                    out var request) || request is null)
                    return;
                object result;
                try { result = _control(request); }
                catch (Exception) { result = new { ok = false, error = "native_action_failed" }; }
                if (NativeBridge.TrustedPage(core.Source, "/companion"))
                    core.PostWebMessageAsJson(JsonSerializer.Serialize(new
                    {
                        channel = NativeBridge.Channel, id = request.Id, result,
                    }));
            };
            await core.AddScriptToExecuteOnDocumentCreatedAsync(BridgeScript.Source);
            core.NavigationCompleted += (_, e) => _ready.TrySetResult(e.IsSuccess
                && NativeBridge.TrustedPage(core.Source, "/companion"));
            core.Navigate(CompanionUri.AbsoluteUri);
        }
        catch (Exception)
        {
            _ready.TrySetResult(false);
        }
    }

    private void Place(PetPosition? saved)
    {
        if (saved is { } position)
        {
            MoveTo(position.X, position.Y);
            ClampIfOffscreen();
        }
        else
        {
            ResetPosition();
        }
    }

    private void MoveTo(double x, double y)
    {
        var handle = new WindowInteropHelper(this).Handle;
        SetWindowPos(handle, 0, (int)Math.Round(x), (int)Math.Round(y), 0, 0,
            NoSize | NoZOrder | NoActivateMove);
    }

    private void TrackPointer()
    {
        if (_closed || !IsVisible)
            return;
        if (_dragging)
        {
            if (GetAsyncKeyState(LeftButton) < 0)
                return;
            _dragging = false;
            ClampIfOffscreen();
            ReportPosition();
        }
        if (!GetCursorPos(out var cursor) || !GetWindowRect(new WindowInteropHelper(this).Handle, out var rect))
            return;
        var frame = new PetRect(rect.Left, rect.Top, rect.Right - rect.Left, rect.Bottom - rect.Top);
        var overPet = CompanionGeometry.PointHitsPet(cursor.X, cursor.Y, frame, _yieldRect);
        SetClickThrough(!overPet);
    }

    private void SetClickThrough(bool enabled)
    {
        if (_clickThrough == enabled)
            return;
        var handle = new WindowInteropHelper(this).Handle;
        var style = GetWindowLongPtr(handle, ExStyle);
        SetWindowLongPtr(handle, ExStyle, enabled ? style | Transparent : style & ~Transparent);
        if (((GetWindowLongPtr(handle, ExStyle) & Transparent) != 0) != enabled)
        {
            HideCompanion();
            return;
        }
        SetWindowPos(handle, 0, 0, 0, 0, 0,
            NoMove | NoSize | NoZOrder | NoActivateMove | FrameChanged);
        _clickThrough = enabled;
    }

    private void ClampIfOffscreen()
    {
        var position = Position();
        GetWindowRect(new WindowInteropHelper(this).Handle, out var bounds);
        var frame = new PetRect(position.X, position.Y,
            bounds.Right - bounds.Left, bounds.Bottom - bounds.Top);
        var screens = Forms.Screen.AllScreens.Select(screen => new PetRect(screen.Bounds.Left,
            screen.Bounds.Top, screen.Bounds.Width, screen.Bounds.Height)).ToArray();
        var clamped = CompanionGeometry.Clamp(frame, screens);
        if (clamped.X != frame.X || clamped.Y != frame.Y)
            MoveTo(clamped.X, clamped.Y);
    }

    private void ReportPosition()
    {
        var position = Position();
        if (position != _lastReported)
        {
            _lastReported = position;
            _positionSettled(position);
        }
    }

    private static nint WindowMessage(nint handle, int message, nint wParam, nint lParam,
        ref bool handled)
    {
        if (message == MouseActivate)
        {
            handled = true;
            return NoActivateResult;
        }
        return 0;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Point { internal int X; internal int Y; }
    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { internal int Left; internal int Top; internal int Right; internal int Bottom; }

    [DllImport("user32.dll")] private static extern nint GetWindowLongPtr(nint hwnd, int index);
    [DllImport("user32.dll")] private static extern nint SetWindowLongPtr(nint hwnd, int index, nint value);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(nint hwnd, nint after, int x, int y,
        int width, int height, uint flags);
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] private static extern bool ReleaseCapture();
    [DllImport("user32.dll")] private static extern nint SendMessage(nint hwnd, int message, nint wParam, nint lParam);
}

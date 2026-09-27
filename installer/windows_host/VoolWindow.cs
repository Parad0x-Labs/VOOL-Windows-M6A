using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Vool.WindowsHost;

internal sealed class VoolWindow : Form
{
    private static readonly Uri ApiOrigin = new("http://127.0.0.1:11435/");
    private readonly byte[] _secret;
    private readonly WebView2 _view = new() { Dock = DockStyle.Fill };
    private static readonly HttpClient ExportClient = new(new SocketsHttpHandler
    {
        UseProxy = false,
        AllowAutoRedirect = false,
    });
    private CoreWebView2Environment? _environment;
    private Form? _settingsWindow;
    private WebView2? _settingsView;
    private Form? _setupWindow;
    private WebView2? _setupView;
    private CompanionWindow? _companion;

    internal VoolWindow(byte[] secret)
    {
        _secret = secret;
        Text = "VOOL";
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(900, 640);
        Size = new Size(1220, 860);
        Controls.Add(_view);
        FormClosed += (_, _) => CloseCompanion();
        Shown += async (_, _) =>
        {
            try
            {
                var profile = NativePathResolver.ResolveWebView2UserDataFolder(
                    Environment.GetEnvironmentVariable("VOOL_HOME"),
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData));
                Directory.CreateDirectory(profile);
                _environment = await CoreWebView2Environment.CreateAsync(userDataFolder: profile);
                await ConfigureView(_view, this, "/chat");
                _view.CoreWebView2.Navigate(new Uri(ApiOrigin, "chat").AbsoluteUri);
            }
            catch (Exception)
            {
                MessageBox.Show(this,
                    "The protected VOOL window could not start. Repair or reinstall VOOL; your data is preserved.",
                    "VOOL could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        };
    }

    private async Task ConfigureView(WebView2 view, Form owner, string expectedPath)
    {
        await view.EnsureCoreWebView2Async(_environment);
        var core = view.CoreWebView2;
        core.Settings.AreHostObjectsAllowed = false;
        core.Settings.IsWebMessageEnabled = true;
        core.Settings.AreDevToolsEnabled = false;
        core.Settings.AreDefaultContextMenusEnabled = false;
        core.NavigationStarting += (_, e) => e.Cancel = !IsLocalPage(e.Uri);
        core.FrameNavigationStarting += (_, e) => e.Cancel = !IsLocalPage(e.Uri);
        core.NewWindowRequested += (_, e) => e.Handled = true;
        core.AddWebResourceRequestedFilter(
            "http://127.0.0.1:11435/api/mode*", CoreWebView2WebResourceContext.All);
        core.AddWebResourceRequestedFilter(
            "http://127.0.0.1:11435/api/commands/dispatch*", CoreWebView2WebResourceContext.All);
        core.WebResourceRequested += (_, e) => OnProtectedRequest(view, owner, e);
        core.WebMessageReceived += (_, e) => OnBridgeMessage(view, owner, expectedPath, e);
        await core.AddScriptToExecuteOnDocumentCreatedAsync(BridgeScript.Source);
    }

    private async void OnBridgeMessage(WebView2 view, Form owner, string expectedPath,
        CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (!NativeBridge.TryParse(e.Source, view.CoreWebView2.Source, expectedPath,
            e.WebMessageAsJson, out var request) || request is null)
            return;

        object result;
        try
        {
            result = request.Method switch
            {
                "open_settings" when expectedPath is "/chat" or "/setup"
                    => OpenPage("/settings", NativeBridge.CleanSection(request.Arguments, false)),
                "open_setup" when expectedPath == "/chat"
                    => OpenPage("/setup", NativeBridge.CleanSection(request.Arguments, true)),
                "close_settings" when expectedPath == "/settings" => ClosePage(owner),
                "close_setup" when expectedPath == "/setup" => ClosePage(owner),
                "pick_folder" when expectedPath is "/chat" or "/setup" => PickFolder(owner),
                "save_answer_pdf" or "save_chat_export" when expectedPath == "/chat"
                    => await SaveExport(owner, request),
                "open_companion_appearance" when expectedPath == "/settings"
                    => await OpenCompanionAppearance(),
                "detach_companion" when expectedPath is "/chat" or "/settings"
                    => await DetachCompanion(request),
                "sync_companion" when expectedPath == "/chat"
                    => await SyncCompanion(request),
                "pet_yield_rect" when expectedPath == "/chat"
                    => YieldCompanion(request),
                "hide_companion" when expectedPath is "/chat" or "/settings"
                    => HideCompanion(),
                "reset_companion_position" when expectedPath is "/chat" or "/settings"
                    => ResetCompanion(),
                "attach_companion" when expectedPath is "/chat" or "/settings"
                    => AttachCompanion(),
                _ => new { ok = false, error = "native_method_unavailable" },
            };
        }
        catch (Exception)
        {
            result = new { ok = false, error = "native_action_failed" };
        }

        if (view.IsDisposed || !NativeBridge.TrustedPage(view.CoreWebView2.Source, expectedPath))
            return;
        try
        {
            view.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new
            {
                channel = NativeBridge.Channel,
                id = request.Id,
                result,
            }));
        }
        catch (InvalidOperationException) { } // The page closed or navigated while a dialog was open.
    }

    private object OpenPage(string path, string section)
    {
        var existing = path == "/settings" ? _settingsWindow : _setupWindow;
        var currentView = path == "/settings" ? _settingsView : _setupView;
        var fragment = section.Length == 0 ? "" : path == "/setup" ? "#step=" + section : "#" + section;
        var url = new Uri(ApiOrigin, path.TrimStart('/') + fragment).AbsoluteUri;
        if (existing is { IsDisposed: false } && currentView is { IsDisposed: false })
        {
            if (section.Length > 0)
            {
                existing.Tag = url;
                currentView.CoreWebView2?.Navigate(url);
            }
            existing.Show();
            if (existing.WindowState == FormWindowState.Minimized)
                existing.WindowState = FormWindowState.Normal;
            existing.BringToFront();
            existing.Activate();
            return new { ok = true, mode = "focused" };
        }

        var child = new Form
        {
            Text = path == "/settings" ? "VOOL Settings" : "VOOL Setup",
            Tag = url,
            StartPosition = FormStartPosition.CenterParent,
            Size = path == "/settings" ? new Size(1000, 720) : new Size(920, 740),
            MinimumSize = new Size(560, path == "/settings" ? 420 : 480),
        };
        var childView = new WebView2 { Dock = DockStyle.Fill };
        child.Controls.Add(childView);
        if (path == "/settings")
        {
            _settingsWindow = child;
            _settingsView = childView;
        }
        else
        {
            _setupWindow = child;
            _setupView = childView;
        }
        child.FormClosed += (_, _) =>
        {
            if (path == "/settings" && ReferenceEquals(_settingsWindow, child))
            {
                _settingsWindow = null;
                _settingsView = null;
            }
            if (path == "/setup" && ReferenceEquals(_setupWindow, child))
            {
                _setupWindow = null;
                _setupView = null;
            }
        };
        child.Shown += async (_, _) =>
        {
            try
            {
                await ConfigureView(childView, child, path);
                if (!child.IsDisposed)
                    childView.CoreWebView2.Navigate((string)child.Tag!);
            }
            catch (Exception)
            {
                if (!child.IsDisposed)
                {
                    MessageBox.Show(child, "This VOOL window could not open. The main chat is unchanged.",
                        "VOOL window unavailable", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    child.Close();
                }
            }
        };
        child.Show(this);
        return new { ok = true, mode = "created" };
    }

    private static object ClosePage(Form page)
    {
        page.BeginInvoke(() => page.Close());
        return new { ok = true };
    }

    private object PickFolder(Form owner)
    {
        using var dialog = new FolderBrowserDialog { ShowNewFolderButton = true };
        return ShowModal(() => dialog.ShowDialog(owner)) == DialogResult.OK
            ? new { ok = true, path = dialog.SelectedPath }
            : (object)new { ok = false, cancelled = true };
    }

    private async Task<object> OpenCompanionAppearance()
    {
        if (_view.IsDisposed || _view.CoreWebView2 is null)
            return new { ok = false, error = "chat_unavailable" };
        var opened = await _view.CoreWebView2.ExecuteScriptAsync(
            "Boolean(window.VoolCompanionDrawer && window.VoolCompanionDrawer.open())");
        if (opened != "true")
            return new { ok = false, error = "appearance_unavailable" };
        if (WindowState == FormWindowState.Minimized)
            WindowState = FormWindowState.Normal;
        BringToFront();
        Activate();
        return new { ok = true };
    }

    private async Task<object> DetachCompanion(BridgeRequest request)
    {
        if (_environment is null)
            return new { ok = false, error = "native_window_unavailable" };
        var payload = CompanionPayload.Normalize(request.Arguments.GetArrayLength() == 0
            ? default : request.Arguments[0]);
        if (_companion?.IsClosed == true)
            _companion = null;
        if (_companion is null)
            _companion = new CompanionWindow(_environment, ReportCompanionPosition, CompanionControl);
        try
        {
            if (await _companion.OpenAsync(payload))
                return new { ok = true, mode = "desktop" };
        }
        catch (Exception) { /* A partial window is never accepted as detached. */ }
        CloseCompanion();
        return new { ok = false, error = "native_window_unavailable" };
    }

    private void CloseCompanion()
    {
        if (_companion is { IsClosed: false } companion)
            companion.Close();
        _companion = null;
    }

    private async Task<object> SyncCompanion(BridgeRequest request)
    {
        if (_companion is null)
            return new { ok = false, error = "not_detached" };
        var payload = CompanionPayload.Normalize(request.Arguments.GetArrayLength() == 0
            ? default : request.Arguments[0]);
        return await _companion.SyncAsync(payload)
            ? new { ok = true } : (object)new { ok = false, error = "native_window_unavailable" };
    }

    private object YieldCompanion(BridgeRequest request)
    {
        if (_companion is null)
            return new { ok = false, error = "pet_not_active" };
        var value = request.Arguments[0];
        if (value.ValueKind == JsonValueKind.Null)
        {
            _companion.SetYieldRect(null);
            return new { ok = true, cleared = true };
        }
        if (!TryNumber(value, "x", out var x) || !TryNumber(value, "y", out var y)
            || !TryNumber(value, "width", out var width) || !TryNumber(value, "height", out var height)
            || !CompanionGeometry.TryYieldRect(x, y, width, height, out var rect))
            return new { ok = false, error = "invalid_rect" };
        _companion.SetYieldRect(rect);
        return new { ok = true };
    }

    private static bool TryNumber(JsonElement value, string name, out double number)
    {
        number = 0;
        return value.TryGetProperty(name, out var field)
            && field.ValueKind == JsonValueKind.Number && field.TryGetDouble(out number);
    }

    private object HideCompanion()
    {
        if (_companion is null)
            return new { ok = false, error = "not_detached" };
        ReportCompanionPosition(_companion.Position());
        _companion.HideCompanion();
        NotifyChat("window.VoolCompanion&&window.VoolCompanion.desktopHidden&&window.VoolCompanion.desktopHidden()");
        return new { ok = true, mode = "hidden" };
    }

    private object AttachCompanion()
    {
        if (_companion is null)
            return new { ok = false, error = "not_detached" };
        ReportCompanionPosition(_companion.Position());
        _companion.HideCompanion();
        NotifyChat("window.VoolCompanion&&window.VoolCompanion.desktopReturned&&window.VoolCompanion.desktopReturned()");
        return new { ok = true, mode = "app" };
    }

    private object ResetCompanion()
    {
        if (_companion is null)
            return new { ok = false, error = "not_detached" };
        var position = _companion.ResetPosition();
        return new { ok = true, position };
    }

    private object CompanionControl(BridgeRequest request) => request.Method switch
    {
        "hide_companion" => HideCompanion(),
        "attach_companion" => AttachCompanion(),
        "reset_companion_position" => ResetCompanion(),
        "begin_companion_drag" => BeginCompanionDrag(),
        _ => new { ok = false, error = "native_method_unavailable" },
    };

    private object BeginCompanionDrag()
    {
        _companion?.BeginDrag();
        return new { ok = true };
    }

    private void ReportCompanionPosition(PetPosition position) => NotifyChat(
        "window.VoolCompanion&&window.VoolCompanion.desktopMoved&&window.VoolCompanion.desktopMoved("
        + JsonSerializer.Serialize(position, new JsonSerializerOptions(JsonSerializerDefaults.Web)) + ")");

    private void NotifyChat(string script)
    {
        if (!_view.IsDisposed && _view.CoreWebView2 is { } core
            && NativeBridge.TrustedPage(core.Source, "/chat"))
            _ = NotifyChatAsync(core, script);
    }

    private static async Task NotifyChatAsync(CoreWebView2 core, string script)
    {
        try { await core.ExecuteScriptAsync(script); }
        catch (Exception) { /* A closing or navigated page cannot accept a position update. */ }
    }

    private async Task<object> SaveExport(Form owner, BridgeRequest bridge)
    {
        var request = NativeExport.Parse(bridge.Method, bridge.Arguments);
        if (request is null)
            return new { ok = false, error = "invalid_export_request" };

        byte[] body;
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        try
        {
            using var response = await ExportClient.GetAsync(request.Url,
                HttpCompletionOption.ResponseHeadersRead, timeout.Token);
            if (!response.IsSuccessStatusCode)
            {
                await using var refusal = await response.Content.ReadAsStreamAsync(timeout.Token);
                var prefix = new byte[4096];
                var length = 0;
                while (length < prefix.Length)
                {
                    var refusalRead = await refusal.ReadAsync(prefix.AsMemory(length), timeout.Token);
                    if (refusalRead == 0) break;
                    length += refusalRead;
                }
                return new { ok = false, error = request.RequestId.Length > 0
                    ? "pdf_export_refused" : "chat_export_refused",
                    message = NativeExport.RefusalMessage(prefix.AsSpan(0, length)) };
            }
            if (response.Content.Headers.ContentType?.MediaType != request.ContentType)
                return new { ok = false, error = "invalid_export_response" };
            await using var stream = await response.Content.ReadAsStreamAsync(timeout.Token);
            using var buffer = new MemoryStream();
            var chunk = new byte[8192];
            int read;
            while ((read = await stream.ReadAsync(chunk, timeout.Token)) != 0)
            {
                if (buffer.Length + read > request.ByteLimit)
                    return new { ok = false, error = "export_too_large",
                        message = "Export exceeds the native save size limit; no file was saved." };
                buffer.Write(chunk, 0, read);
            }
            body = buffer.ToArray();
        }
        catch (OperationCanceledException)
        {
            return new { ok = false, error = "export_timeout", message = "Export timed out; try again." };
        }
        catch (HttpRequestException)
        {
            return new { ok = false, error = "export_unavailable", message = "VOOL export is unavailable; try again." };
        }
        if (!NativeExport.ValidBody(request, request.ContentType, body))
            return new { ok = false, error = "invalid_export_response" };

        using var dialog = new SaveFileDialog
        {
            FileName = request.FileName,
            Filter = request.Format switch
            {
                "pdf" => "PDF documents (*.pdf)|*.pdf",
                "md" => "Markdown documents (*.md)|*.md",
                _ => "Text documents (*.txt)|*.txt",
            },
            AddExtension = true,
            CheckPathExists = true,
            OverwritePrompt = false,
        };
        if (ShowModal(() => dialog.ShowDialog(owner)) != DialogResult.OK)
            return new { ok = false, cancelled = true };
        try
        {
            if (File.Exists(dialog.FileName))
                return new { ok = false, error = "destination_exists",
                    message = "Choose a new filename; the existing file was not changed." };
            NativeExport.SaveNoClobber(body, dialog.FileName);
            return new { ok = true };
        }
        catch (IOException)
        {
            return new { ok = false, error = "export_save_failed",
                message = "Export could not be saved. Check the destination and try again." };
        }
        catch (UnauthorizedAccessException)
        {
            return new { ok = false, error = "export_save_denied",
                message = "VOOL cannot write to that location. Choose another folder." };
        }
    }

    private static bool IsLocalPage(string raw)
    {
        return Uri.TryCreate(raw, UriKind.Absolute, out var uri)
            && uri.Scheme == ApiOrigin.Scheme
            && uri.Host == ApiOrigin.Host
            && uri.Port == ApiOrigin.Port;
    }

    private DialogResult ShowModal(Func<DialogResult> show)
    {
        var petWasVisible = _companion?.IsVisible == true;
        if (petWasVisible) _companion!.HideCompanion();
        try { return show(); }
        finally { if (petWasVisible) _companion?.Resume(); }
    }

    private void OnProtectedRequest(WebView2 view, Form owner, CoreWebView2WebResourceRequestedEventArgs e)
    {
        if (!Uri.TryCreate(e.Request.Uri, UriKind.Absolute, out var uri)
            || !NativeControl.IsProtected(uri, e.Request.Method))
            return;

        try
        {
            var body = ReadBody(e.Request.Content);
            e.Request.Content = new MemoryStream(body, writable: false);
            var request = NativeControl.Review(uri, e.Request.Method, body);
            if (request is null)
            {
                Refuse(view, e, "Unknown or invalid native control request.");
                return;
            }
            if (request.NeedsConfirmation)
            {
                using var dialog = new ApprovalDialog(uri.PathAndQuery, request);
                if (ShowModal(() => dialog.ShowDialog(owner)) != DialogResult.OK)
                {
                    Refuse(view, e, "Native approval was denied.");
                    return;
                }
            }
            foreach (var (name, value) in NativeControl.Proof(
                _secret, e.Request.Method, uri.PathAndQuery, body))
                e.Request.Headers.SetHeader(name, value);
        }
        catch (Exception)
        {
            Refuse(view, e, "Native approval could not be verified.");
        }
    }

    private static byte[] ReadBody(Stream? stream)
    {
        if (stream is null)
            return [];
        using var buffer = new MemoryStream();
        var chunk = new byte[8192];
        int read;
        while ((read = stream.Read(chunk)) != 0)
        {
            if (buffer.Length + read > NativeControl.MaxBodyBytes)
                throw new InvalidDataException("Native control request exceeds the body limit.");
            buffer.Write(chunk, 0, read);
        }
        return buffer.ToArray();
    }

    private static void Refuse(WebView2 view, CoreWebView2WebResourceRequestedEventArgs e, string reason)
    {
        var json = System.Text.Json.JsonSerializer.Serialize(new { error = reason });
        e.Response = view.CoreWebView2.Environment.CreateWebResourceResponse(
            new MemoryStream(Encoding.UTF8.GetBytes(json)), 403, "Forbidden",
            "Content-Type: application/json\r\nCache-Control: no-store");
    }
}

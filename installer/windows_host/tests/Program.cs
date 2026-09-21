using System.Text;
using System.Text.Json;
using Vool.WindowsHost;

static void Check(bool condition, string name)
{
    if (!condition)
        throw new Exception(name);
}

var origin = "http://127.0.0.1:11435";
var mode = new Uri(origin + "/api/mode?source=window");
var dispatch = new Uri(origin + "/api/commands/dispatch");
Check(NativeControl.IsProtected(mode, "POST"), "mode requires proof");
Check(NativeControl.IsProtected(dispatch, "post"), "dispatch requires proof");
Check(!NativeControl.IsProtected(new Uri("http://localhost:11435/api/mode"), "POST"), "exact host");
Check(!NativeControl.IsProtected(new Uri("https://127.0.0.1:11435/api/mode"), "POST"), "exact scheme");
Check(!NativeControl.IsProtected(new Uri(origin + "/api/mode/extra"), "POST"), "exact path");

ReviewRequest? Review(Uri uri, string json) => NativeControl.Review(uri, "POST", Encoding.UTF8.GetBytes(json));
Check(Review(mode, "" ) is null, "empty refusal");
Check(Review(mode, "[]") is null, "object required");
Check(Review(mode, "{\"op\":\"set\",\"mode\":\"auto\",\"mode\":\"manual\"}") is null,
    "duplicate key refusal");
Check(Review(dispatch, "{\"command_id\":\"update.check\",\"input\":{\"target\":\"A\",\"target\":\"B\"}}") is null,
    "nested duplicate key refusal");
Check(Review(mode, "{\"op\":\"set\",\"mode\":\"bogus\"}") is null, "unknown mode refusal");
Check(Review(mode, "{\"op\":\"set\",\"mode\":\"auto\"}")?.NeedsConfirmation == true,
    "auto approval");
Check(Review(mode, "{\"op\":\"activate_bypass\"}")?.NeedsConfirmation == true,
    "bypass approval");
Check(Review(mode, "{\"op\":\"resolve_approval\",\"decision\":\"allow\"}")?.NeedsConfirmation == true,
    "allow approval");
Check(Review(mode, "{\"op\":\"grant_chat_workspace\"}")?.NeedsConfirmation == true,
    "workspace approval");
Check(Review(dispatch, "{\"command_id\":\"update.check\",\"input\":{}}")?.NeedsConfirmation == true,
    "command approval");
Check(Review(dispatch, "{\"input\":{}}") is null, "unknown command refusal");
Check(Review(mode, "{\"op\":\"revoke_bypass\"}")?.NeedsConfirmation == false,
    "revocation");
Check(Review(mode, "{\"op\":\"set\",\"mode\":\"manual\"}")?.NeedsConfirmation == false,
    "manual mode");
Check(Review(mode, "{\"op\":\"resolve_approval\",\"decision\":\"deny\"}")?.NeedsConfirmation == false,
    "deny action");
Check(NativeControl.Review(mode, "POST", new byte[NativeControl.MaxBodyBytes + 1]) is null,
    "oversized refusal");

var bridgeJson = "{\"channel\":\"vool-native-v1\",\"id\":\"a1\",\"method\":\"open_settings\",\"args\":[\"memory\"]}";
Check(NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat", bridgeJson, out var bridge)
    && bridge?.Method == "open_settings" && NativeBridge.CleanSection(bridge.Arguments, false) == "memory",
    "trusted bridge request");
Check(!NativeBridge.TryParse("http://127.0.0.1:11436/chat", origin + "/chat", "/chat", bridgeJson, out _),
    "foreign port refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/settings", "/chat", bridgeJson, out _),
    "navigated page refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    bridgeJson.Replace("open_settings", "run_command"), out _), "unknown method refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    bridgeJson.Replace("\"args\":", "\"id\":\"b2\",\"args\":"), out _), "duplicate key refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    bridgeJson.Replace("\"vool-native-v1\"", "7"), out _), "non-string channel refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    bridgeJson.Replace("[\"memory\"]", "[\"memory\",\"models\"]"), out _), "extra arguments refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    bridgeJson.Replace("[\"memory\"]", "[]"), out _), "missing arguments refusal");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    bridgeJson + new string(' ', NativeBridge.MaxMessageBytes), out _), "oversized message refusal");
Check(NativeBridge.CleanSection(bridge!.Arguments, false) == "memory", "clean section");
Check(BridgeScript.Source.Contains("crypto.getRandomValues", StringComparison.Ordinal),
    "bridge request IDs must not collide across page reloads");
Check(NativeBridge.TryParse(origin + "/settings", origin + "/settings", "/settings",
    "{\"channel\":\"vool-native-v1\",\"id\":\"a2\",\"method\":\"open_companion_appearance\",\"args\":[]}",
    out _), "appearance bridge request");
Check(NativeBridge.TryParse(origin + "/settings", origin + "/settings", "/settings",
    "{\"channel\":\"vool-native-v1\",\"id\":\"p1\",\"method\":\"detach_companion\",\"args\":[]}",
    out _), "settings can request a default companion");
Check(NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    "{\"channel\":\"vool-native-v1\",\"id\":\"p2\",\"method\":\"detach_companion\",\"args\":[{\"state\":\"tool\"}]}",
    out _), "chat can send typed companion state");
Check(NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    "{\"channel\":\"vool-native-v1\",\"id\":\"p3\",\"method\":\"pet_yield_rect\",\"args\":[null]}",
    out _), "chat can clear companion yield rect");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    "{\"channel\":\"vool-native-v1\",\"id\":\"p4\",\"method\":\"pet_yield_rect\",\"args\":[\"all\"]}",
    out _), "yield rect rejects untyped data");
Check(!NativeBridge.TryParse(origin + "/chat", origin + "/chat", "/chat",
    "{\"channel\":\"vool-native-v1\",\"id\":\"p5\",\"method\":\"attach_companion\",\"args\":[true]}",
    out _), "companion control rejects extra arguments");
foreach (var method in new[] { "detach_companion", "sync_companion", "pet_yield_rect",
    "hide_companion", "reset_companion_position", "attach_companion" })
    Check(BridgeScript.Source.Contains(method + ":", StringComparison.Ordinal),
        "Companion bridge exposes " + method);
Check(NativeBridge.TryParse(origin + "/companion", origin + "/companion", "/companion",
    "{\"channel\":\"vool-native-v1\",\"id\":\"d1\",\"method\":\"begin_companion_drag\",\"args\":[]}",
    out _), "companion canvas drag request");
Check(!NativeBridge.TryParse(origin + "/companion", origin + "/companion", "/companion",
    "{\"channel\":\"vool-native-v1\",\"id\":\"d2\",\"method\":\"begin_companion_drag\",\"args\":[1]}",
    out _), "drag refuses extra arguments");

var primary = new PetRect(0, 0, 1920, 1080);
var secondary = new PetRect(-1600, 0, 1600, 900);
var pet = CompanionGeometry.DefaultFrame(primary);
Check(pet == new PetRect(1696, 808, 176, 176), "lower-right primary placement");
Check(CompanionGeometry.PointHitsPet(pet.X + 88, pet.Y + 88, pet), "canvas hit");
Check(CompanionGeometry.PointHitsCanvas(pet.X + 88, pet.Y + 88, pet), "canvas drag hit");
Check(!CompanionGeometry.PointHitsCanvas(pet.X + 150, pet.Y + 12, pet), "control cannot start drag");
Check(CompanionGeometry.PointHitsPet(pet.X + 150, pet.Y + 12, pet), "control hit");
Check(CompanionGeometry.PointHitsPet(pet.X + 102, pet.Y + 12, pet), "whole control strip hit");
Check(!CompanionGeometry.PointHitsPet(pet.X + 8, pet.Y + 8, pet), "transparent margin click-through");
Check(CompanionGeometry.PointHitsPet(pet.X + 204, pet.Y + 24,
    new PetRect(pet.X, pet.Y, 352, 352)), "scaled control strip hit");
Check(!CompanionGeometry.PointHitsPet(pet.X + 88, pet.Y + 88, pet,
    new PetRect(pet.X + 40, pet.Y + 40, 96, 96)), "answer region click-through");
Check(CompanionGeometry.PointHitsPet(pet.X + 150, pet.Y + 12, pet,
    new PetRect(pet.X + 40, pet.Y + 40, 96, 96)), "control outside answer region remains live");
Check(CompanionGeometry.Clamp(new PetRect(-20, 100, 176, 176), [primary, secondary])
    == new PetRect(-20, 100, 176, 176), "valid monitor straddle remains stable");
Check(CompanionGeometry.Clamp(new PetRect(30000, 30000, 176, 176), [primary, secondary])
    == new PetRect(1744, 904, 176, 176), "lost pet recovered on nearest display");
Check(CompanionGeometry.Clamp(new PetRect(-1800, 100, 176, 176), [primary, secondary])
    == new PetRect(-1600, 100, 176, 176), "negative-origin display recovery");
Check(!CompanionGeometry.TryYieldRect(0, 0, double.NaN, 10, out _), "NaN yield rect refused");
Check(!CompanionGeometry.TryYieldRect(0, 0, 20001, 10, out _), "oversize yield rect refused");
Check(CompanionGeometry.TryYieldRect(-100, 250, 200, 40, out var yieldRect)
    && yieldRect == new PetRect(-100, 250, 200, 40), "top-left Windows yield rect retained");
using var petPayload = JsonDocument.Parse("{\"state\":\"tool\",\"character\":\"prism\",\"pack\":\"cyber\",\"caption\":\"</script><b>bad</b>\",\"desktop_position\":{\"x\":-1200,\"y\":400}}");
var cleanPet = CompanionPayload.Normalize(petPayload.RootElement);
Check(cleanPet.State == "tool" && cleanPet.Character == "prism" && cleanPet.Pack == "cyber",
    "companion presentation allowlist");
Check(cleanPet.DesktopPosition == new PetPosition(-1200, 400), "companion position retained");
Check(!cleanPet.ToRendererJson().Contains("</script>", StringComparison.OrdinalIgnoreCase)
    && cleanPet.ToRendererJson().Contains("\\u003C", StringComparison.Ordinal),
    "companion renderer JSON cannot close script");
using var unsafePayload = JsonDocument.Parse("{\"state\":\"execute\",\"character\":\"root\",\"pack\":\"other\",\"desktop_position\":{\"x\":60001,\"y\":0}}");
var safePet = CompanionPayload.Normalize(unsafePayload.RootElement);
Check(safePet.State == "unknown" && safePet.Character == "spark" && safePet.Pack == "default"
    && safePet.DesktopPosition is null, "invalid companion data normalizes safely");
using var longCaption = JsonDocument.Parse("{\"caption\":\"" + new string('a', 100) + "\"}");
Check(CompanionPayload.Normalize(longCaption.RootElement).Caption.Length == 64, "companion caption bound");

using var answerArgs = JsonDocument.Parse("[\"chat #1\",\"request/2\"]");
var answer = NativeExport.Parse("save_answer_pdf", answerArgs.RootElement);
Check(answer is not null && answer.Url.Query.Contains("session=chat%20%231")
    && answer.Url.Query.Contains("request_id=request%2F2") && answer.FileName == "vool-answer.pdf",
    "answer identity and encoded URL");
Check(!NativeExport.ValidBody(answer!, "application/pdf", "not-a-pdf"u8.ToArray()), "PDF signature");
Check(NativeExport.ValidBody(answer!, "application/pdf", "%PDF-1.7"u8.ToArray()), "valid PDF");
using var chatArgs = JsonDocument.Parse("[\"chat\",\"md\",true,false]");
var chatExport = NativeExport.Parse("save_chat_export", chatArgs.RootElement);
Check(chatExport is not null && chatExport.Url.Query.Contains("timestamps=1")
    && chatExport.Url.Query.Contains("no_attachments=1") && chatExport.ByteLimit == 16 * 1024 * 1024,
    "chat export options");
using var badArgs = JsonDocument.Parse("[\"chat\",\"exe\",true,false]");
Check(NativeExport.Parse("save_chat_export", badArgs.RootElement) is null, "unknown format refusal");
Check(NativeExport.RefusalMessage("{\"message\":\"Export withheld\"}"u8) == "Export withheld",
    "typed export refusal");
Check(NativeExport.RefusalMessage("<html>error</html>"u8) == "Export refused by VOOL.",
    "untrusted refusal fallback");

var exportDir = Path.Combine(Path.GetTempPath(), "vool-export-check-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(exportDir);
try
{
    var target = Path.Combine(exportDir, "chat.md");
    NativeExport.SaveNoClobber("first"u8.ToArray(), target);
    Check(File.ReadAllText(target) == "first", "native export save");
    try
    {
        NativeExport.SaveNoClobber("second"u8.ToArray(), target);
        throw new Exception("no-clobber refusal");
    }
    catch (IOException) { }
    Check(File.ReadAllText(target) == "first", "native export preserves existing file");
    Check(Directory.GetFiles(exportDir).Length == 1, "temporary export cleanup");
}
finally
{
    Directory.Delete(exportDir, recursive: true);
}

var proof = NativeControl.Proof(
    Enumerable.Range(0, NativeControl.SecretBytes).Select(n => (byte)n).ToArray(),
    "POST", mode.PathAndQuery, Encoding.UTF8.GetBytes("{\"op\":\"set\",\"mode\":\"auto\"}"),
    nonce: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", issuedAt: long.Parse(args[0]));
Console.WriteLine(JsonSerializer.Serialize(proof));

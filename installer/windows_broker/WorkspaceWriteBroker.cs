using System.Globalization;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

namespace Vool.WindowsBroker;

internal static class WorkspaceWriteBroker
{
    private const string Schema = "vool.workspace_write.v1";
    private const string Effect = "workspace_write_v1";
    private const int MaximumContentBytes = 32 * 1024 * 1024;
    internal const FileSystemRights WorkspaceParentStagingRights = FileSystemRights.Modify;

    internal static async Task<WorkspaceWriteExecutionResult> ExecuteAsync(
        WorkspaceWriteRequest request,
        CancellationToken cancellationToken = default,
        string? workerExecutablePath = null,
        FileSystemRights? workspaceParentRightsOverride = null,
        Action? beforeParentGrantTestHook = null,
        Action? afterParentGrantTestHook = null,
        Action? beforeTargetAclVerificationTestHook = null,
        Action? beforeTargetAclRestorationTestHook = null)
    {
        EnsureSupported();
        var paths = ValidateRequest(request);
        var before = ReadTarget(paths.TargetPath);
        EnsurePriorState(request, before);
        using var priorAcl = request.PriorState == "file" ? WindowsDacl.Capture(paths.TargetPath) : null;
        if (priorAcl is not null && priorAcl.Identity != before.Identity)
            throw new BrokerSecurityException("The workspace target changed before ACL capture.");
        var originalTargetAcl = priorAcl?.OriginalAcl;
        var originalTargetOwner = priorAcl?.Original.Owner?.Value;
        if (priorAcl is not null && !priorAcl.HasUnambiguousFileEncoding())
            return Failure(request, before, "refused",
                "The workspace target has an ambiguous legacy DACL encoding; refused before mutation.");

        var workerPath = workerExecutablePath ?? Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(workerPath)
            || !Path.GetExtension(workerPath).Equals(".exe", StringComparison.OrdinalIgnoreCase))
        {
            return Failure(request, before, "confinement_unavailable", "The packaged broker worker executable was not available.");
        }

        workerPath = PathSafety.RequireLocalFile(workerPath, "workspace broker executable");
        var workerDirectory = Path.GetDirectoryName(workerPath)
            ?? throw new BrokerSecurityException("Workspace broker executable has no parent directory.");
        var normalizedWorkerDirectory = TrimDirectorySeparator(Path.GetFullPath(workerDirectory));
        if (PathSafety.IsWithin(normalizedWorkerDirectory, paths.TargetParent)
            || PathSafety.IsWithin(paths.TargetParent, normalizedWorkerDirectory))
        {
            throw new BrokerSecurityException(
                "The workspace target parent must not overlap the broker executable directory.");
        }
        var staging = CreateStagingContext(paths.TargetParent, paths.TargetPath, request.OperationId);
        var stagingPath = staging.FilePath;
        if (request.PriorState == "absent")
        {
            originalTargetAcl = staging.TemplateAcl;
            originalTargetOwner = staging.TemplateOwner;
        }

        try
        {
            var workerInput = JsonSerializer.Serialize(request, JsonOptions);
            using var appContainer = AppContainerSession.Create(
                "workspace-write",
                "v1",
                request.ApprovedEffectIdentity,
                request.OperationId);
            beforeParentGrantTestHook?.Invoke();
            var stagingRights = workspaceParentRightsOverride
                ?? WorkspaceParentStagingRights;
            var directoryGrants = BuildWorkspaceGrantRequests(
                staging.DirectoryPath,
                workerDirectory,
                stagingRights);
            using var grants = WindowsPathGrants.CreateWithRules(appContainer.Sid, directoryGrants);
            afterParentGrantTestHook?.Invoke();
            EnsureTargetIdentity(paths.TargetPath, before.Identity);
            using var job = WindowsJob.Create();
            using var process = NativeProcessLauncher.Start(
                appContainer.SidPointer,
                appContainer.Sid.Value,
                job,
                workerPath,
                new[] { "workspace-write-worker" },
                staging.DirectoryPath,
                BuildWorkerEnvironment(
                    workerDirectory,
                    staging.DirectoryPath,
                    appContainer.Sid.Value),
                workerInput);

        var processResult = await process.WaitAsync(
                job.Handle,
                TimeSpan.FromMilliseconds(request.TimeoutMilliseconds),
                cancellationToken)
            .ConfigureAwait(false);
        WorkspaceWriteWorkerResult workerResult;
        try
        {
            workerResult = ParseWorkerResult(processResult.Stdout);
        }
        catch (BrokerSecurityException exception)
        {
            workerResult = new WorkspaceWriteWorkerResult(
                false,
                processResult.TimedOut ? "timeout" : "handler_failed",
                before.State,
                before.Sha256,
                "",
                0,
                false,
                exception.Message);
        }
        var status = processResult.TimedOut
            ? "timeout"
            : workerResult.Ok && processResult.ExitCode == 0
                ? "executed"
                : workerResult.Status is "stale_base" or "destination_changed" or "refused"
                    ? workerResult.Status
                    : "handler_failed";

        var after = ReadTarget(paths.TargetPath);
        var hostInstalled = false;
        string? expectedInstalledAcl = null;
        string? hostInstallError = null;
        if (workerResult.Ok
            && !processResult.TimedOut
            && processResult.ExitCode == 0)
        {
            try
            {
                EnsurePriorState(request, after);
                EnsureTargetIdentity(paths.TargetPath, before.Identity);
                VerifyStagedContent(stagingPath, request.ContentSha256);
                if (priorAcl is not null)
                {
                    priorAcl.RequireBoundPath();
                    var live = priorAcl.Read();
                    if (live.GetSddlForm(AccessControlSections.Access) != priorAcl.OriginalAcl
                        || live.Owner?.Value != priorAcl.Original.Owner?.Value)
                        throw new BrokerSecurityException("Foreign target ACL drift before installation.");
                }
                using var stagedAcl = WindowsDacl.Capture(stagingPath);
                ValidateStagedAcl(stagedAcl, staging, appContainer.Sid, stagingRights);
                if (stagedAcl.Original.Owner?.Value != originalTargetOwner)
                    throw new BrokerSecurityException("The staged owner does not match the approved target owner.");
                if (originalTargetAcl is null)
                    throw new BrokerSecurityException("The approved target has no captured DACL.");
                // Worker authority ends before the host commits. Removing a
                // file ACE alone is insufficient while its parent still grants
                // it: filesystem inheritance/ReplaceFile can reintroduce it.
                grants.Dispose();
                stagedAcl.RequireBoundPath();
                var grantFree = stagedAcl.Read();
                if (grantFree.Owner?.Value != staging.TemplateOwner
                    || !WindowsDacl.EquivalentTransition(grantFree, new RawSecurityDescriptor(staging.TemplateAcl)))
                    throw new BrokerSecurityException("Grant-free staged ACL does not match its approved template.");
                stagedAcl.Restore(originalTargetAcl, originalTargetOwner,
                    grantFree.GetSddlForm(AccessControlSections.Access));
                expectedInstalledAcl = originalTargetAcl;
                if (request.PriorState == "absent")
                    File.Move(stagingPath, paths.TargetPath, overwrite: false);
                else
                    File.Replace(
                        stagingPath,
                        paths.TargetPath,
                        destinationBackupFileName: null,
                        ignoreMetadataErrors: true);
                hostInstalled = true;
                after = ReadTarget(paths.TargetPath);
            }
            catch (Exception exception)
            {
                hostInstallError =
                    $"The approved absent target could not be installed atomically: {exception.Message}";
            }
        }
        var verified = hostInstalled && workerResult.Ok
            && !processResult.TimedOut
            && processResult.ExitCode == 0
            && after.State == "file"
            && string.Equals(after.Sha256, request.ContentSha256, StringComparison.OrdinalIgnoreCase);
        if (hostInstallError is not null)
            verified = false;
        string? cleanupError = null;
        if (hostInstalled && originalTargetAcl is not null
            && after.State == "file"
            && string.Equals(after.Sha256, request.ContentSha256, StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                if (after.Identity is null)
                    throw new BrokerSecurityException("The final target had no verifiable file identity.");
                beforeTargetAclRestorationTestHook?.Invoke();
                RestoreTargetAcl(paths.TargetPath, after.Identity, originalTargetAcl,
                    originalTargetOwner, expectedInstalledAcl);
                after = ReadTarget(paths.TargetPath);
                verified = verified
                    && string.Equals(after.Sha256, request.ContentSha256, StringComparison.OrdinalIgnoreCase);
            }
            catch (Exception exception)
            {
                cleanupError =
                    $"Mutation occurred, but exact target ACL restoration failed; remediation required: {exception.Message}";
                verified = false;
            }
        }

        try
        {
            grants.Dispose();
        }
        catch (Exception exception)
        {
            cleanupError ??= $"Temporary filesystem grant cleanup failed; remediation required: {exception.Message}";
            verified = false;
        }

        try
        {
            CleanupStagingDirectory(staging);
        }
        catch (Exception exception)
        {
            cleanupError ??= $"Workspace staging cleanup failed; remediation required: {exception.Message}";
            verified = false;
        }

        if (cleanupError is null
            && originalTargetAcl is not null
            && after.State == "file"
            && string.Equals(after.Sha256, request.ContentSha256, StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                beforeTargetAclVerificationTestHook?.Invoke();
                VerifyTargetAcl(paths.TargetPath, originalTargetAcl);
            }
            catch (Exception exception)
            {
                cleanupError =
                    $"Mutation occurred, but exact target ACL restoration failed; remediation required: {exception.Message}";
                verified = false;
            }
        }

        if (cleanupError is not null)
        {
            return BuildResult(
                request,
                before,
                after,
                ok: false,
                status: "security_cleanup_failed",
                processResult,
                process,
                appContainer,
                grants,
                job,
                cleanupError,
                atomicInstalled: false);
        }
        if (!verified)
        {
            status = workerResult.Ok ? "verification_failed" : status;
            if (hostInstallError is not null)
                status = "handler_failed";
            return BuildResult(
                request,
                before,
                after,
                ok: false,
                status,
                processResult,
                process,
                appContainer,
                grants,
                job,
                hostInstallError ?? workerResult.Error,
                atomicInstalled: false);
        }

        return BuildResult(
            request,
            before,
            after,
            ok: true,
            status: "executed",
            processResult,
            process,
            appContainer,
            grants,
            job,
            error: "",
            atomicInstalled: workerResult.AtomicInstalled || hostInstalled);
        }
        catch
        {
            try
            {
                CleanupStagingDirectory(staging);
            }
            catch
            {
                // Preserve the original broker failure. The failed staging path remains visible
                // for remediation rather than being silently treated as a successful operation.
            }
            throw;
        }
    }

    internal static async Task<WorkspaceAuthorityProbeExecution> ProbeParentAuthorityAsync(
        WorkspaceWriteRequest request,
        string workerExecutablePath,
        IReadOnlyList<WorkspaceAuthorityProbeOperation> operations,
        CancellationToken cancellationToken = default)
    {
        EnsureSupported();
        var paths = ValidateRequest(request);
        var before = ReadTarget(paths.TargetPath);
        EnsurePriorState(request, before);

        var workerPath = PathSafety.RequireLocalFile(workerExecutablePath, "workspace authority probe executable");
        var workerDirectory = Path.GetDirectoryName(workerPath)
            ?? throw new BrokerSecurityException("Workspace authority probe executable has no parent directory.");
        var normalizedWorkerDirectory = TrimDirectorySeparator(Path.GetFullPath(workerDirectory));
        if (PathSafety.IsWithin(normalizedWorkerDirectory, paths.TargetParent)
            || PathSafety.IsWithin(paths.TargetParent, normalizedWorkerDirectory))
        {
            throw new BrokerSecurityException(
                "The workspace target parent must not overlap the authority probe executable directory.");
        }

        var probeArguments = new List<string> { "probe" };
        foreach (var operation in operations)
            AppendAuthorityProbeArguments(probeArguments, operation);

        var staging = CreateStagingContext(paths.TargetParent, paths.TargetPath, request.OperationId);
        try
        {
            using var appContainer = AppContainerSession.Create(
                "workspace-write",
                "v1",
                request.ApprovedEffectIdentity,
                request.OperationId);
            var directoryGrants = BuildWorkspaceGrantRequests(
                staging.DirectoryPath,
                workerDirectory,
                WorkspaceParentStagingRights);
            using var grants = WindowsPathGrants.CreateWithRules(appContainer.Sid, directoryGrants);
            using var job = WindowsJob.Create();
            var environment = new Dictionary<string, string>(
                BuildWorkerEnvironment(
                    workerDirectory,
                    staging.DirectoryPath,
                    appContainer.Sid.Value),
                StringComparer.OrdinalIgnoreCase)
            {
                ["VOOL_WORKSPACE_AUTHORITY_PROBE"] = "1",
            };
            using var process = NativeProcessLauncher.Start(
                appContainer.SidPointer,
                appContainer.Sid.Value,
                job,
                workerPath,
                probeArguments,
                staging.DirectoryPath,
                environment);
            var processResult = await process.WaitAsync(
                    job.Handle,
                    TimeSpan.FromMilliseconds(request.TimeoutMilliseconds),
                    cancellationToken)
                .ConfigureAwait(false);
            var execution = new WorkspaceAuthorityProbeExecution(
                processResult,
                process.ProcessId,
                process.JobOwned,
                appContainer.Name,
                appContainer.Sid.Value,
                process.TokenSecurity,
                job.Security,
                grants.Evidence);
            grants.Dispose();
            CleanupStagingDirectory(staging);
            return execution;
        }
        catch
        {
            try
            {
                CleanupStagingDirectory(staging);
            }
            catch
            {
                // Preserve the probe failure and leave residue visible for remediation.
            }
            throw;
        }
    }

    internal static WorkspaceWriteWorkerResult RunWorker(WorkspaceWriteRequest request)
    {
        var expectedAppContainerSid = Environment.GetEnvironmentVariable("VOOL_WORKSPACE_WRITE_APP_CONTAINER_SID");
        if (string.IsNullOrWhiteSpace(expectedAppContainerSid))
            throw new BrokerSecurityException("Workspace write worker has no operation-bound AppContainer identity.");
        TokenInspector.RequireWorkerToken(TokenInspector.InspectCurrentProcess(), expectedAppContainerSid);
        var paths = ValidateRequest(request, requireAccessibleDirectories: false);
        var before = new TargetState(request.PriorState, request.PriorSha256);
        var content = DecodeContent(request);
        var stagingDirectory = GetStagingDirectoryFromEnvironment(paths.TargetParent);
        var tempPath = GetStagingPath(stagingDirectory, paths.TargetPath, request.OperationId);
        var preserveStagedContent = false;

        try
        {
            using (var staged = new FileStream(
                       tempPath,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None,
                       64 * 1024,
                       FileOptions.WriteThrough | FileOptions.SequentialScan))
            {
                staged.Write(content, 0, content.Length);
                staged.Flush(flushToDisk: true);
            }

            var stagedHash = HashFile(tempPath);
            if (!string.Equals(stagedHash, request.ContentSha256, StringComparison.OrdinalIgnoreCase))
            {
                return WorkerFailure("content_hash_mismatch", "The staged content hash did not match the approved content.", before);
            }

            preserveStagedContent = true;
            return new WorkspaceWriteWorkerResult(
                true,
                "staged",
                before.State,
                before.Sha256,
                request.ContentSha256,
                content.LongLength,
                false);
        }
        catch (BrokerSecurityException exception)
        {
            return WorkerFailure("refused", exception.Message, before);
        }
        catch (IOException exception)
        {
            return WorkerFailure("destination_changed", exception.Message, before);
        }
        catch (UnauthorizedAccessException exception)
        {
            return WorkerFailure("refused", exception.Message, before);
        }
        finally
        {
            try
            {
                if (!preserveStagedContent && File.Exists(tempPath))
                    File.Delete(tempPath);
            }
            catch
            {
                // The parent reports the operation as failed if the worker cannot verify the final
                // state. Never turn cleanup failure into a success claim.
            }
        }
    }

    private static WorkspaceWriteWorkerResult WorkerFailure(
        string status,
        string error,
        TargetState before) => new(
        false,
        status,
        before.State,
        before.Sha256,
        "",
        0,
        false,
        error);

    private static IReadOnlyList<WindowsPathGrantRequest> BuildWorkspaceGrantRequests(
        string stagingDirectory,
        string workerDirectory,
        FileSystemRights parentStagingRights) =>
        new[]
        {
            new WindowsPathGrantRequest(
                stagingDirectory,
                parentStagingRights,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None),
            new WindowsPathGrantRequest(
                workerDirectory,
                FileSystemRights.ReadAndExecute,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None),
        };

    private static void AppendAuthorityProbeArguments(
        ICollection<string> arguments,
        WorkspaceAuthorityProbeOperation operation)
    {
        var option = operation.Name switch
        {
            "read" => "--workspace-authority-read",
            "overwrite" => "--workspace-authority-overwrite",
            "append" => "--workspace-authority-append",
            "delete" => "--workspace-authority-delete",
            "rename" => "--workspace-authority-rename",
            "replace" => "--workspace-authority-replace",
            "truncate" => "--workspace-authority-truncate",
            "attributes" => "--workspace-authority-attributes",
            "security" => "--workspace-authority-security",
            _ => throw new BrokerSecurityException(
                $"Unsupported workspace authority probe operation: {operation.Name}"),
        };
        arguments.Add(option);
        arguments.Add(PathSafety.RequireLocalPath(operation.TargetPath, "authority probe target"));
        if (operation.Name is "rename" or "replace")
        {
            if (string.IsNullOrWhiteSpace(operation.SecondaryPath))
                throw new BrokerSecurityException(
                    $"Authority probe operation {operation.Name} requires a secondary path.");
            arguments.Add(PathSafety.RequireLocalPath(operation.SecondaryPath, "authority probe secondary target"));
        }
    }

    private static WorkspaceWriteExecutionResult BuildResult(
        WorkspaceWriteRequest request,
        TargetState before,
        TargetState after,
        bool ok,
        string status,
        NativeProcessResult processResult,
        NativeProcessLauncher process,
        AppContainerSession appContainer,
        WindowsPathGrants grants,
        WindowsJob job,
        string error,
        bool atomicInstalled)
    {
        var evidence = new WorkspaceWriteConfinementEvidence(
            "windows",
            "appcontainer_job_object",
            Effect,
            request.Tool,
            request.ApprovalId,
            request.ApprovalStatus,
            request.ProposalId,
            request.OperationId,
            request.RuntimeId,
            TrimDirectorySeparator(Path.GetFullPath(request.WorkspaceRoot)),
            request.RelativeTarget,
            Path.GetFullPath(request.TargetPath),
            before.State,
            before.Sha256,
            request.ContentSha256,
            request.ContentLength,
            appContainer.Name,
            appContainer.Sid.Value,
            process.ProcessId,
            process.JobOwned,
            grants.Evidence,
            "none",
            request.TimeoutMilliseconds,
            status,
            ok,
            processResult.ExitCode,
            processResult.TimedOut,
            process.TokenSecurity,
            job.Security);
        return new WorkspaceWriteExecutionResult(
            ok,
            status,
            before.State,
            before.Sha256,
            after.Sha256,
            request.ContentSha256,
            request.ContentLength,
            atomicInstalled,
            error,
            evidence);
    }

    private static WorkspaceWriteExecutionResult Failure(
        WorkspaceWriteRequest request,
        TargetState before,
        string status,
        string error) => new(
        false,
        status,
        before.State,
        before.Sha256,
        before.Sha256,
        request.ContentSha256,
        request.ContentLength,
        false,
        error,
        null);

    private static (string WorkspaceRoot, string TargetPath, string TargetParent) ValidateRequest(
        WorkspaceWriteRequest request,
        bool requireAccessibleDirectories = true)
    {
        if (request is null)
            throw new ArgumentNullException(nameof(request));
        if (!string.Equals(request.Schema, Schema, StringComparison.Ordinal)
            || !string.Equals(request.Effect, Effect, StringComparison.Ordinal))
        {
            throw new BrokerSecurityException("Unsupported workspace write broker request schema or effect.");
        }

        if (request.Tool is not ("workspace.write_file" or "workspace.replace_in_file"))
            throw new BrokerSecurityException("Unsupported workspace write tool.");
        if (request.ApprovalStatus is not ("consumed" or "policy_allowed"))
            throw new BrokerSecurityException("Workspace write approval status is not valid.");

        RequireBindingText(request.ApprovalId, "approval_id");
        RequireBindingText(request.ProposalId, "proposal_id");
        RequireBindingText(request.OperationId, "operation_id");
        RequireBindingText(request.RuntimeId, "runtime_id");
        if (!IsSha256(request.ApprovedEffectIdentity))
            throw new BrokerSecurityException("approved_effect_identity must be a SHA-256 value.");
        if (!IsSha256(request.ContentSha256))
            throw new BrokerSecurityException("content_sha256 must be a SHA-256 value.");
        if (request.PriorState is not ("file" or "absent"))
            throw new BrokerSecurityException("prior_state must be file or absent.");
        if (request.PriorState == "absent" && !string.IsNullOrEmpty(request.PriorSha256))
            throw new BrokerSecurityException("An absent prior target must not carry a prior hash.");
        if (request.PriorState == "file" && !IsSha256(request.PriorSha256))
            throw new BrokerSecurityException("A file prior target must carry a SHA-256 value.");
        if (request.TimeoutMilliseconds <= 0
            || request.TimeoutMilliseconds > BrokerLimits.MaximumTimeout.TotalMilliseconds)
        {
            throw new BrokerSecurityException("Workspace write timeout is outside the bounded policy.");
        }

        var content = DecodeContent(request);
        if (content.LongLength != request.ContentLength)
            throw new BrokerSecurityException("content_length does not match the supplied content.");
        if (!string.Equals(
                Convert.ToHexString(SHA256.HashData(content)),
                request.ContentSha256,
                StringComparison.OrdinalIgnoreCase))
        {
            throw new BrokerSecurityException("content_sha256 does not match the supplied content.");
        }

        var workspaceRoot = TrimDirectorySeparator(
            requireAccessibleDirectories
                ? PathSafety.RequireLocalDirectory(request.WorkspaceRoot, "workspace root")
                : PathSafety.RequireLocalPath(request.WorkspaceRoot, "workspace root"));
        var targetPath = PathSafety.RequireLocalPath(request.TargetPath, "workspace write target");
        var targetParent = Path.GetDirectoryName(targetPath)
            ?? throw new BrokerSecurityException("Workspace write target has no parent directory.");
        targetParent = TrimDirectorySeparator(
            requireAccessibleDirectories
                ? PathSafety.RequireLocalDirectory(targetParent, "workspace write target parent")
                : PathSafety.RequireLocalPath(targetParent, "workspace write target parent"));
        if (!PathSafety.IsWithin(workspaceRoot, targetPath)
            || !PathSafety.IsWithin(workspaceRoot, targetParent))
        {
            throw new BrokerSecurityException("Workspace write target escaped the active workspace.");
        }
        if (Directory.Exists(targetPath))
            throw new BrokerSecurityException("Workspace write target is a directory.");

        var relative = Path.GetRelativePath(workspaceRoot, targetPath).Replace('\\', '/');
        if (string.IsNullOrWhiteSpace(relative)
            || relative == "."
            || relative.StartsWith("../", StringComparison.Ordinal)
            || relative.Contains(':')
            || !string.Equals(relative, request.RelativeTarget.Replace('\\', '/'), StringComparison.Ordinal))
        {
            throw new BrokerSecurityException("relative_target is not the canonical target within workspace_root.");
        }

        var expectedBinding = ComputeBindingIdentity(request, workspaceRoot, relative);
        if (!string.Equals(expectedBinding, request.ApprovedEffectIdentity, StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("approved_effect_identity does not bind this exact workspace write.");
        return (workspaceRoot, targetPath, targetParent);
    }

    private static void EnsurePriorState(WorkspaceWriteRequest request, TargetState actual)
    {
        if (!string.Equals(actual.State, request.PriorState, StringComparison.Ordinal)
            || !string.Equals(actual.Sha256, request.PriorSha256, StringComparison.OrdinalIgnoreCase))
        {
            throw new BrokerSecurityException("The workspace write target changed after approval.");
        }
    }

    private static string TrimDirectorySeparator(string path)
    {
        var root = Path.GetPathRoot(path);
        if (!string.IsNullOrEmpty(root)
            && string.Equals(path, root, StringComparison.OrdinalIgnoreCase))
        {
            return root;
        }
        return path.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }

    private static TargetState ReadTarget(string targetPath)
    {
        PathSafety.RejectReparseAncestors(targetPath);
        if (Directory.Exists(targetPath))
            return new TargetState("directory", "");
        if (!File.Exists(targetPath))
            return new TargetState("absent", "");
        var attributes = File.GetAttributes(targetPath);
        if ((attributes & FileAttributes.ReparsePoint) != 0)
            throw new BrokerSecurityException("Workspace write target is a reparse point.");
        using var stream = new FileStream(
            targetPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.ReadWrite | FileShare.Delete,
            64 * 1024,
            FileOptions.SequentialScan);
        var identity = GetFileIdentity(stream.SafeFileHandle);
        var hash = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        return new TargetState("file", hash, identity);
    }

    private static string ReadTargetAcl(string targetPath) =>
        new FileInfo(targetPath)
            .GetAccessControl(AccessControlSections.Access)
            .GetSecurityDescriptorSddlForm(AccessControlSections.Access);

    private static void RestoreTargetAcl(
        string targetPath,
        string expectedIdentity,
        string originalAcl,
        string? expectedOwner,
        string? expectedInstalledAcl)
    {
        using var target = WindowsDacl.Capture(targetPath);
        if (!string.Equals(target.Identity, expectedIdentity, StringComparison.Ordinal))
        {
            throw new BrokerSecurityException("The workspace write target identity changed during ACL restoration.");
        }
        target.Restore(originalAcl, expectedOwner, expectedInstalledAcl);
    }

    private static void ValidateStagedAcl(WindowsDacl staged, WorkspaceStagingContext staging,
        SecurityIdentifier sid, FileSystemRights rights)
    {
        staged.RequireBoundPath();
        var descriptor = staged.Read();
        var allowedMask = (int)new FileSystemAccessRule(sid, rights, AccessControlType.Allow).FileSystemRights;
        var acl = descriptor.DiscretionaryAcl
            ?? throw new BrokerSecurityException("Staged file has no DACL.");
        var removed = 0;
        for (var index = acl.Count - 1; index >= 0; index--)
        {
            if (acl[index] is not CommonAce ace || ace.SecurityIdentifier != sid)
                continue;
            if (ace.AceQualifier != AceQualifier.AccessAllowed || ace.AceFlags != AceFlags.Inherited
                || ace.AccessMask != allowedMask)
                throw new BrokerSecurityException("Staged file has an unexpected operation grant.");
            acl.RemoveAce(index);
            removed++;
        }
        if (removed != 1 || descriptor.Owner?.Value != staging.TemplateOwner
            || !WindowsDacl.EquivalentTransition(descriptor, new RawSecurityDescriptor(staging.TemplateAcl)))
            throw new BrokerSecurityException("Staged file ACL does not match its approved template and operation grant.");
    }

    private static void VerifyTargetAcl(string targetPath, string originalAcl)
    {
        var restoredAcl = ReadTargetAcl(targetPath);
        if (!string.Equals(restoredAcl, originalAcl, StringComparison.Ordinal))
        {
            throw new BrokerSecurityException("The final target ACL did not match the approved original ACL.");
        }
    }

    private static void EnsureTargetIdentity(string targetPath, string? expectedIdentity)
    {
        if (expectedIdentity is null)
            return;
        var actual = ReadTarget(targetPath);
        if (!string.Equals(actual.Identity, expectedIdentity, StringComparison.Ordinal))
        {
            throw new BrokerSecurityException("The workspace write target identity changed during the operation.");
        }
    }

    private static string GetFileIdentity(SafeFileHandle handle)
    {
        if (!NativeMethods.GetFileInformationByHandleEx(
                handle,
                NativeMethods.FileIdInfoClass,
                out var information,
                (uint)System.Runtime.InteropServices.Marshal.SizeOf<NativeMethods.FileIdInfo>())
            || information.FileId is null)
        {
            throw NativeMethods.LastError("GetFileInformationByHandleEx");
        }
        return $"{information.VolumeSerialNumber:X16}:{Convert.ToHexString(information.FileId)}";
    }

    private static string HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 64 * 1024);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    private static void VerifyStagedContent(string path, string expectedHash)
    {
        if (!File.Exists(path) || (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new BrokerSecurityException("The operation-bound staged content was not a regular file.");
        var actualHash = HashFile(path);
        if (!string.Equals(actualHash, expectedHash, StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("The operation-bound staged content hash did not match the approved content.");
    }

    private static WorkspaceStagingContext CreateStagingContext(
        string targetParent,
        string targetPath,
        string operationId)
    {
        var operationToken = OperationToken(operationId);
        var directoryPath = Path.Combine(
            targetParent,
            $".{Path.GetFileName(targetPath)}.vool-write-{operationToken}");
        if (File.Exists(directoryPath) || Directory.Exists(directoryPath))
            throw new BrokerSecurityException("The operation-bound workspace staging path already exists.");

        Directory.CreateDirectory(directoryPath);
        try
        {
            PathSafety.RejectReparseAncestors(directoryPath);
            // This directory is newly created and operation-owned. Preserve all
            // entries but stop later parent inheritance from changing its template.
            var directory = new DirectoryInfo(directoryPath);
            var directoryAcl = directory.GetAccessControl(AccessControlSections.Access);
            directoryAcl.SetAccessRuleProtection(isProtected: true, preserveInheritance: true);
            directory.SetAccessControl(directoryAcl);
            var templatePath = Path.Combine(directoryPath, ".acl-template");
            File.WriteAllBytes(templatePath, Array.Empty<byte>());
            var templateAcl = ReadTargetAcl(templatePath);
            var templateOwner = new FileInfo(templatePath).GetAccessControl(AccessControlSections.Owner)
                .GetOwner(typeof(SecurityIdentifier))?.Value
                ?? throw new BrokerSecurityException("The staging template has no verifiable owner.");
            File.Delete(templatePath);
            var filePath = GetStagingPath(directoryPath, targetPath, operationId);
            return new WorkspaceStagingContext(directoryPath, filePath, templateAcl, templateOwner);
        }
        catch
        {
            try
            {
                if (Directory.Exists(directoryPath))
                    Directory.Delete(directoryPath, recursive: true);
            }
            catch
            {
                // Preserve the original staging setup failure and leave residue visible for remediation.
            }
            throw;
        }
    }

    private static void CleanupStagingDirectory(WorkspaceStagingContext staging)
    {
        if (!Directory.Exists(staging.DirectoryPath))
        {
            if (File.Exists(staging.DirectoryPath))
                throw new BrokerSecurityException("The workspace staging path became a file.");
            return;
        }

        foreach (var entry in Directory.EnumerateFileSystemEntries(staging.DirectoryPath))
        {
            if (!string.Equals(
                    Path.GetFullPath(entry),
                    Path.GetFullPath(staging.FilePath),
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new BrokerSecurityException(
                    $"Unexpected workspace staging residue requires remediation: {entry}");
            }
            if ((File.GetAttributes(entry) & FileAttributes.ReparsePoint) != 0)
                throw new BrokerSecurityException("The workspace staging residue was a reparse point.");
            File.Delete(entry);
        }

        Directory.Delete(staging.DirectoryPath, recursive: false);
    }

    private static string GetStagingDirectoryFromEnvironment(string targetParent)
    {
        var value = Environment.GetEnvironmentVariable("VOOL_WORKSPACE_WRITE_STAGING_DIRECTORY");
        if (string.IsNullOrWhiteSpace(value))
            throw new BrokerSecurityException("Workspace write worker has no operation-bound staging directory.");
        var stagingDirectory = TrimDirectorySeparator(
            PathSafety.RequireLocalDirectory(value, "workspace write staging directory"));
        var stagingParent = Path.GetDirectoryName(stagingDirectory)
            ?? throw new BrokerSecurityException("Workspace write staging directory has no parent.");
        if (!string.Equals(
                TrimDirectorySeparator(Path.GetFullPath(stagingParent)),
                TrimDirectorySeparator(Path.GetFullPath(targetParent)),
                StringComparison.OrdinalIgnoreCase))
        {
            throw new BrokerSecurityException(
                "Workspace write staging directory is not the operation-bound target-parent child.");
        }
        return stagingDirectory;
    }

    private static string GetStagingPath(string stagingDirectory, string targetPath, string operationId) =>
        Path.Combine(
            stagingDirectory,
            $".{Path.GetFileName(targetPath)}.vool-write-{OperationToken(operationId)}.tmp");

    private static string OperationToken(string operationId) => Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(operationId)))
        .ToLowerInvariant()[..24];

    private static byte[] DecodeContent(WorkspaceWriteRequest request)
    {
        byte[] content;
        try
        {
            content = Convert.FromBase64String(request.ContentBase64 ?? "");
        }
        catch (FormatException exception)
        {
            throw new BrokerSecurityException("content_base64 is not valid base64.", exception);
        }
        if (content.Length > MaximumContentBytes)
            throw new BrokerSecurityException("Workspace write content exceeds the bounded size.");
        return content;
    }

    private static string ComputeBindingIdentity(
        WorkspaceWriteRequest request,
        string workspaceRoot,
        string relativeTarget) => Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(string.Join(
                "\n",
                Schema,
                Effect,
                request.Tool,
                request.ApprovalId,
                request.ProposalId,
                request.OperationId,
                request.RuntimeId,
                workspaceRoot,
                relativeTarget,
                request.PriorState,
                request.PriorSha256,
                request.ContentSha256,
                request.ContentLength.ToString(CultureInfo.InvariantCulture))))).ToLowerInvariant();

    private static void EnsureSupported()
    {
        var support = WindowsExecutionBroker.GetSupportReport();
        if (!support.Supported)
            throw new BrokerSecurityException($"Refusing workspace write: {support.Reason}");
    }

    private static void RequireBindingText(string value, string name)
    {
        if (string.IsNullOrWhiteSpace(value)
            || value.Length > 512
            || value.Contains('\0')
            || value.Contains('\r')
            || value.Contains('\n'))
        {
            throw new BrokerSecurityException($"Invalid workspace write binding field: {name}.");
        }
    }

    private static bool IsSha256(string value) =>
        !string.IsNullOrWhiteSpace(value)
        && value.Length == 64
        && value.All(Uri.IsHexDigit);

    private static IReadOnlyDictionary<string, string> BuildWorkerEnvironment(
        string workerDirectory,
        string stagingDirectory,
        string appContainerSid)
    {
        var systemRoot = Environment.GetEnvironmentVariable("SystemRoot");
        if (string.IsNullOrWhiteSpace(systemRoot))
            throw new BrokerSecurityException("SystemRoot is unavailable; refusing workspace write.");
        var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SystemRoot"] = systemRoot,
            ["WINDIR"] = systemRoot,
            ["SystemDrive"] = Path.GetPathRoot(systemRoot)?.TrimEnd(Path.DirectorySeparatorChar) ?? "C:",
            ["PATH"] = string.Join(';', workerDirectory, Path.Combine(systemRoot, "System32"), systemRoot),
            ["TEMP"] = stagingDirectory,
            ["TMP"] = stagingDirectory,
            ["LOCALAPPDATA"] = stagingDirectory,
            ["APPDATA"] = stagingDirectory,
            ["USERPROFILE"] = stagingDirectory,
            ["HOME"] = stagingDirectory,
            ["VOOL_BROKER_APP_CONTAINER"] = "1",
            ["VOOL_WORKSPACE_WRITE_WORKER"] = "1",
            ["VOOL_WORKSPACE_WRITE_APP_CONTAINER_SID"] = appContainerSid,
            ["VOOL_WORKSPACE_WRITE_STAGING_DIRECTORY"] = stagingDirectory,
            ["PROCESSOR_ARCHITECTURE"] = "AMD64",
        };
        return environment;
    }

    private static WorkspaceWriteWorkerResult ParseWorkerResult(string stdout)
    {
        try
        {
            return JsonSerializer.Deserialize<WorkspaceWriteWorkerResult>(stdout, JsonOptions)
                ?? throw new BrokerSecurityException("Workspace write worker returned no result.");
        }
        catch (JsonException exception)
        {
            throw new BrokerSecurityException("Workspace write worker returned invalid JSON.", exception);
        }
    }

    private sealed record TargetState(string State, string Sha256, string? Identity = null);

    private sealed record WorkspaceStagingContext(
        string DirectoryPath,
        string FilePath,
        string TemplateAcl,
        string TemplateOwner);

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };
}

internal sealed record WorkspaceAuthorityProbeOperation(
    string Name,
    string TargetPath,
    string? SecondaryPath = null);

internal sealed record WorkspaceAuthorityProbeExecution(
    NativeProcessResult ProcessResult,
    int ProcessId,
    bool JobOwned,
    string AppContainerName,
    string AppContainerSid,
    TokenSecuritySnapshot TokenSecurity,
    JobSecuritySnapshot JobSecurity,
    IReadOnlyList<FilesystemGrantEvidence> FilesystemGrants);

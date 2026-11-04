using VoolConfine.Core;
using Xunit;

namespace VoolConfine.Tests;

public class PolicyTests
{
    const string Good = """
        {"schema":"vool.windows_confinement.v1","write_roots":["C:\\work\\repo"],
         "temp_dir":"C:\\work\\repo\\.vool-tmp","status_path":"C:\\tmp\\vc\\status.json","network":false,"max_seconds":120}
        """;

    [Fact]
    public void A_valid_policy_parses_with_the_documented_defaults()
    {
        var p = Policy.Parse(Good);
        Assert.Equal(@"C:\work\repo", Assert.Single(p.WriteRoots));
        Assert.True(p.DenyWriteGit);
        Assert.False(p.Network);
        Assert.Equal(512, p.MemoryMb);
        Assert.Equal(64, p.MaxProcesses);
    }

    [Theory]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":[],"temp_dir":"C:\\t"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w"],"temp_dir":"D:\\elsewhere"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["\\\\server\\share"],"temp_dir":"\\\\server\\share\\t"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w\\..\\x"],"temp_dir":"C:\\w\\..\\x\\t"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["relative"],"temp_dir":"relative\\t"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w"],"temp_dir":"C:\\w\\t","status_path":"C:\\s\\status.json","surprise":1}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v2","write_roots":["C:\\w"],"temp_dir":"C:\\w\\t","status_path":"C:\\s\\status.json"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w"],"temp_dir":"C:\\w\\t","status_path":"C:\\s\\status.json","network":"yes"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w"],"temp_dir":"C:\\w\\t","status_path":"C:\\s\\status.json","max_seconds":0}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w"],"temp_dir":"C:\\w\\t"}""")]
    [InlineData("""{"schema":"vool.windows_confinement.v1","write_roots":["C:\\w"],"temp_dir":"C:\\w\\t","status_path":"C:\\w\\status.json"}""")]
    [InlineData("""not json""")]
    public void A_policy_that_breaks_a_rule_is_refused(string json)
    {
        Assert.Throws<PolicyException>(() => Policy.Parse(json));
    }
}

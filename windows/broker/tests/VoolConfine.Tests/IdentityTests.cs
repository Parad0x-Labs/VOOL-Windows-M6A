using VoolConfine.Core;
using Xunit;

namespace VoolConfine.Tests;

public class IdentityTests
{
    // Same vector as tests/test_windows_confinement_interface.py: Python and C# must agree.
    [Fact]
    public void The_root_capability_name_matches_the_python_side()
    {
        const string expected = "vool.root.b144b923dd859cdb6402070dce0592f6319ce623a8e197cd4a45fb96585d7e40";
        Assert.Equal(expected, Identity.RootCapabilityName(@"C:\work\a"));
        Assert.Equal(expected, Identity.RootCapabilityName("c:/work/a/"));
        Assert.NotEqual(expected, Identity.RootCapabilityName(@"C:\work\b"));
    }
}

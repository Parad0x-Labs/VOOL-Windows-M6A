using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

public sealed class RootsTests
{
    [Fact]
    public void Relative_paths_reject_root_escape()
    {
        Assert.Equal("path_escapes_root", Assert.Throws<BrokerRefusal>(() => HandleWalk.ValidateRelativePath("a\\..\\b")).Code);
    }
}

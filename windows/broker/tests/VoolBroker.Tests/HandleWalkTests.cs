using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

public sealed class HandleWalkTests
{
    [Theory]
    [InlineData("CON", "device_name")]
    [InlineData("folder/file:stream", "invalid_name")]
    [InlineData("folder./file", "invalid_name")]
    public void Refuses_invalid_components(string path, string code)
    {
        Assert.Equal(code, Assert.Throws<BrokerRefusal>(() => HandleWalk.ValidateRelativePath(path)).Code);
    }
}

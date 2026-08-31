using Xunit;

namespace LintJail.Smoke;

public class SmokeTests
{
    [Fact]
    public void JailRunsTests()
    {
        Assert.Equal(4, 2 + 2);
    }
}

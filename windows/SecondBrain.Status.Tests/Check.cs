namespace SecondBrain.Status.Tests;

static class Check
{
    public static void That(bool condition, string what)
    {
        if (!condition) throw new Exception($"Check failed: {what}");
    }

    public static void Equal<T>(T expected, T actual, string what)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
            throw new Exception($"Check failed: {what}: expected <{expected}>, got <{actual}>");
    }

    public static void Throws<TException>(Action action, string what) where TException : Exception
    {
        try { action(); }
        catch (TException) { return; }
        throw new Exception($"Check failed: {what}: expected {typeof(TException).Name}");
    }
}

defmodule TournamentUiWeb.SafeMarkdownTest do
  use ExUnit.Case, async: true

  alias TournamentUiWeb.SafeMarkdown

  defp render(md), do: md |> SafeMarkdown.render() |> Phoenix.HTML.safe_to_string()

  describe "injection is neutralized" do
    test "script tags are dropped including their content" do
      html = render("before\n\n<script>alert('xss')</script>\n\nafter")

      refute html =~ "<script"
      refute html =~ "alert("
      assert html =~ "before"
      assert html =~ "after"
    end

    test "inline script inside a paragraph is escaped, never executable" do
      html = render("hello <script>alert(1)</script> world")

      refute html =~ "<script"
      assert html =~ "hello"
      assert html =~ "world"
    end

    test "img tags are dropped entirely (no onerror surface)" do
      # Block-level raw <img> arrives as a verbatim AST node — dropped.
      html = render("before\n\n<img src=x onerror=alert(1)>\n\n![alt](https://x.y/i.png)")

      refute html =~ "<img"
      refute html =~ "onerror"
      assert html =~ "before"
    end

    test "inline img is neutralized (escaped text, never an element)" do
      html = render("look <img src=x onerror=alert(1)> here")

      refute html =~ "<img"
      assert html =~ "&lt;img"
      assert html =~ "look"
      assert html =~ "here"
    end

    test "javascript: href is rejected, anchor unwrapped to text" do
      html = render("[click me](javascript:alert(1))")

      refute html =~ "javascript:"
      refute html =~ "<a"
      assert html =~ "click me"
    end

    test "data: and vbscript: hrefs are rejected" do
      html =
        render("""
        [d](data:text/html;base64,PHNjcmlwdD4=) [v](vbscript:msgbox) [D](DATA:text/html,x)
        """)

      refute html =~ "<a"
      refute html =~ "data:"
      refute html =~ "vbscript:"
      assert html =~ "d"
      assert html =~ "v"
    end

    test "scheme check is case/whitespace insensitive" do
      html = render("[j](JaVaScRiPt:alert(1)) [ok]( HTTPS://example.com )")

      refute html =~ "avascript"
      assert html =~ ~s(href="HTTPS://example.com")
    end

    test "event-handler attributes are stripped from raw HTML blocks" do
      html = render("<div onclick=\"steal()\" onmouseover=\"x\">content</div>")

      refute html =~ "onclick"
      refute html =~ "onmouseover"
      assert html =~ "content"
    end

    test "event handlers and disallowed attrs are stripped from allowed tags" do
      html = render(~s(<p onclick="x" style="position:fixed" id="a">text</p>))

      refute html =~ "onclick"
      refute html =~ "position:fixed"
      refute html =~ ~s(id=")
      assert html =~ "text"
    end

    test "iframe/object/embed/form/style are dropped with content" do
      md = """
      keep me

      <iframe src="https://evil.example"></iframe>

      <object data="x"></object>

      <embed src="x">

      <form action="https://evil.example"><input name="pw"></form>

      <style>body { display: none }</style>
      """

      html = render(md)

      for tag <- ~w(iframe object embed form style input) do
        refute html =~ "<#{tag}", "expected no <#{tag}> in output"
      end

      refute html =~ "evil.example"
      refute html =~ "display: none"
      assert html =~ "keep me"
    end

    test "malformed / unclosed HTML mixed into markdown cannot break out" do
      md = """
      ## Heading

      Unclosed <b>bold and <script>alert(1) with *emphasis* trailing

      <div><span>nested unclosed
      """

      html = render(md)

      refute html =~ "<script"
      refute html =~ "<b>"
      assert html =~ "<h2>"
      assert html =~ "Heading"
      assert html =~ "<em>emphasis</em>"
    end

    test "anchors never keep extra attributes and always force rel" do
      html = render(~s(<a href="https://ok.example" onclick="x" target="_top">go</a>))

      assert html =~ ~s(href="https://ok.example")
      assert html =~ ~s(rel="noopener noreferrer")
      assert html =~ ~s(target="_blank")
      refute html =~ "onclick"
      refute html =~ "_top"
    end

    test "nil renders as empty" do
      assert Phoenix.HTML.safe_to_string(SafeMarkdown.render(nil)) == ""
    end
  end

  describe "legitimate work-order markdown still renders" do
    test "h2 headings, lists, and inline emphasis" do
      md = """
      ## Goal

      Harden the **release** pipeline.

      ## Implementation plan

      1. Fix it
      2. Test it

      - alpha
      - beta
      """

      html = render(md)

      assert html =~ "<h2>"
      assert html =~ "Goal"
      assert html =~ "<strong>release</strong>"
      assert html =~ "<ol>"
      assert html =~ "<ul>"
      assert html =~ "<li>"
      assert html =~ "Test it"
    end

    test "fenced code blocks render as pre/code" do
      md = """
      ```elixir
      def hello, do: :world
      ```
      """

      html = render(md)

      assert html =~ "<pre>"
      assert html =~ "<code"
      assert html =~ "def hello, do: :world"
    end

    test "https links keep working with forced rel" do
      html = render("[repo](https://github.com/decentraland/unity-explorer)")

      assert html =~ ~s(<a href="https://github.com/decentraland/unity-explorer")
      assert html =~ ~s(rel="noopener noreferrer")
      assert html =~ "repo</a>"
    end

    test "mailto links are allowed" do
      html = render("[mail](mailto:team@example.com)")

      assert html =~ ~s(href="mailto:team@example.com")
    end

    test "blockquotes, hr, and tables render" do
      md = """
      > quoted wisdom

      ---

      | a | b |
      | - | - |
      | 1 | 2 |
      """

      html = render(md)

      assert html =~ "<blockquote>"
      assert html =~ "quoted wisdom"
      assert html =~ "<hr"
      assert html =~ "<table>"
      assert html =~ "<td"
    end

    test "text content is HTML-escaped" do
      html = render("compare a < b && b > c")

      assert html =~ "&lt;"
      refute html =~ "< b &&"
    end
  end

  describe "safe_link?/1" do
    test "accepts only https URLs" do
      assert SafeMarkdown.safe_link?("https://github.com/x/y")
      refute SafeMarkdown.safe_link?("http://github.com/x/y")
      refute SafeMarkdown.safe_link?("javascript:alert(1)")
      refute SafeMarkdown.safe_link?("data:text/html,x")
      refute SafeMarkdown.safe_link?(nil)
      refute SafeMarkdown.safe_link?(%{})
    end
  end
end

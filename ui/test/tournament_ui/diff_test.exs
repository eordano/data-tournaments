defmodule TournamentUi.DiffTest do
  use ExUnit.Case, async: true

  # Pure parser over UNTRUSTED candidate-authored diff text: correct
  # add/del/ctx gutter numbering, rename/new/deleted headers, no-newline
  # markers, malformed input degrading (never crashing), and the per-file
  # line cap flagging truncation while keeping counts honest.

  alias TournamentUi.Diff

  @modified """
  diff --git a/lib/foo.ex b/lib/foo.ex
  index 1111111..2222222 100644
  --- a/lib/foo.ex
  +++ b/lib/foo.ex
  @@ -1,4 +1,5 @@
   defmodule Foo do
  -  def a, do: 1
  +  def a, do: 2
  +  def b, do: 3
   end
  """

  test "modified file: status, counts, and ctx/add/del line numbering" do
    assert [file] = Diff.parse_unified(@modified)

    assert file.path_old == "lib/foo.ex"
    assert file.path_new == "lib/foo.ex"
    assert file.status == :modified
    assert file.additions == 2
    assert file.deletions == 1
    refute file.truncated?

    assert [hunk] = file.hunks
    assert hunk.header == "@@ -1,4 +1,5 @@"
    assert hunk.old_start == 1
    assert hunk.new_start == 1

    assert [
             %{type: :ctx, old_no: 1, new_no: 1, text: "defmodule Foo do"},
             %{type: :del, old_no: 2, new_no: nil, text: "  def a, do: 1"},
             %{type: :add, old_no: nil, new_no: 2, text: "  def a, do: 2"},
             %{type: :add, old_no: nil, new_no: 3, text: "  def b, do: 3"},
             %{type: :ctx, old_no: 3, new_no: 4, text: "end"}
           ] = hunk.lines
  end

  test "new file: /dev/null old side → :added" do
    text = """
    diff --git a/NEW.md b/NEW.md
    new file mode 100644
    index 0000000..e69de29
    --- /dev/null
    +++ b/NEW.md
    @@ -0,0 +1,2 @@
    +hello
    +world
    """

    assert [file] = Diff.parse_unified(text)
    assert file.status == :added
    assert file.path_new == "NEW.md"
    assert file.additions == 2
    assert file.deletions == 0
    assert [%{lines: [%{type: :add, new_no: 1}, %{type: :add, new_no: 2}]}] = file.hunks
  end

  test "deleted file: /dev/null new side → :deleted" do
    text = """
    diff --git a/OLD.md b/OLD.md
    deleted file mode 100644
    --- a/OLD.md
    +++ /dev/null
    @@ -1,2 +0,0 @@
    -bye
    -now
    """

    assert [file] = Diff.parse_unified(text)
    assert file.status == :deleted
    assert file.path_new == nil
    assert file.deletions == 2
    assert [%{lines: [%{type: :del, old_no: 1}, %{type: :del, old_no: 2}]}] = file.hunks
  end

  test "rename headers carry both paths and :renamed status" do
    text = """
    diff --git a/lib/old_name.ex b/lib/new_name.ex
    similarity index 95%
    rename from lib/old_name.ex
    rename to lib/new_name.ex
    """

    assert [file] = Diff.parse_unified(text)
    assert file.status == :renamed
    assert file.path_old == "lib/old_name.ex"
    assert file.path_new == "lib/new_name.ex"
    assert file.hunks == []
  end

  test "no-newline marker renders as a :meta line with no gutter numbers" do
    text = """
    diff --git a/a.txt b/a.txt
    --- a/a.txt
    +++ b/a.txt
    @@ -1 +1 @@
    -old
    \\ No newline at end of file
    +new
    \\ No newline at end of file
    """

    assert [file] = Diff.parse_unified(text)
    assert [hunk] = file.hunks

    metas = Enum.filter(hunk.lines, &(&1.type == :meta))
    assert length(metas) == 2
    assert Enum.all?(metas, &(&1.old_no == nil and &1.new_no == nil))
    assert hd(metas).text =~ "No newline"
    # Counts unaffected by meta lines.
    assert file.additions == 1
    assert file.deletions == 1
  end

  test "multiple files parse in order" do
    text =
      @modified <>
        """
        diff --git a/README.md b/README.md
        --- a/README.md
        +++ b/README.md
        @@ -1 +1,2 @@
         # readme
        +new line
        """

    assert [a, b] = Diff.parse_unified(text)
    assert a.path_new == "lib/foo.ex"
    assert b.path_new == "README.md"
    assert b.additions == 1
    assert b.deletions == 0
  end

  test "malformed input degrades to [] — never a crash" do
    assert Diff.parse_unified("complete garbage\nno headers here\n+++ stray") == []
    assert Diff.parse_unified("") == []
    assert Diff.parse_unified(nil) == []
    assert Diff.parse_unified(%{not: "a string"}) == []
  end

  test "garbage inside a hunk becomes opaque :meta lines, never a crash" do
    text = """
    diff --git a/a.txt b/a.txt
    --- a/a.txt
    +++ b/a.txt
    @@ -1 +1 @@
    -old
    ~~~corrupted line~~~
    +new
    """

    assert [file] = Diff.parse_unified(text)
    assert [hunk] = file.hunks
    assert Enum.any?(hunk.lines, &(&1.type == :meta and &1.text =~ "corrupted"))
    assert file.additions == 1
    assert file.deletions == 1
  end

  test "unparseable @@ header inside a file degrades to a meta line" do
    text = """
    diff --git a/a.txt b/a.txt
    --- a/a.txt
    +++ b/a.txt
    @@ mangled hunk header @@
    +orphan add
    """

    # No valid hunk ever opened; nothing to attach body lines to, but the
    # file itself still parses (with zero stored hunks) — no crash.
    assert [file] = Diff.parse_unified(text)
    assert file.path_new == "a.txt"
  end

  test "per-file line cap marks the file truncated but keeps honest counts" do
    adds = Enum.map_join(1..(Diff.file_line_cap() + 50), "\n", fn i -> "+line #{i}" end)

    text =
      """
      diff --git a/big.txt b/big.txt
      --- /dev/null
      +++ b/big.txt
      @@ -0,0 +1,#{Diff.file_line_cap() + 50} @@
      """ <> adds <> "\n"

    assert [file] = Diff.parse_unified(text)
    assert file.truncated?
    # Counts stay honest past the cap...
    assert file.additions == Diff.file_line_cap() + 50
    # ...while stored lines stop at the cap.
    stored = file.hunks |> Enum.flat_map(& &1.lines) |> length()
    assert stored == Diff.file_line_cap()
  end

  test "binary file section is flagged, not exploded" do
    text = """
    diff --git a/img.png b/img.png
    index 1111111..2222222 100644
    Binary files a/img.png and b/img.png differ
    """

    assert [file] = Diff.parse_unified(text)
    assert file.binary?
    assert file.hunks == []
  end

  test "duplicate paths yield two distinct file entries (index-keyed, no merge)" do
    # A malicious/degenerate diff can repeat the same path. The parser must
    # keep them as SEPARATE entries so the renderer's index-based DOM ids
    # (#diff-file-N) never collide — nothing is keyed by the raw path.
    text = """
    diff --git a/dup.ex b/dup.ex
    --- a/dup.ex
    +++ b/dup.ex
    @@ -1 +1 @@
    -one
    +uno
    diff --git a/dup.ex b/dup.ex
    --- a/dup.ex
    +++ b/dup.ex
    @@ -5 +5 @@
    -five
    +cinco
    """

    assert [first, second] = Diff.parse_unified(text)
    assert first.path_new == "dup.ex"
    assert second.path_new == "dup.ex"
    assert first.additions == 1 and second.additions == 1
    assert [%{header: "@@ -1 +1 @@"}] = first.hunks
    assert [%{header: "@@ -5 +5 @@"}] = second.hunks
  end
end

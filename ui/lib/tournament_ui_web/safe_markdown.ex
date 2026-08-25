defmodule TournamentUiWeb.SafeMarkdown do
  @moduledoc """
  Renders untrusted markdown (LLM output, and soon multi-source external
  context: GitHub issues, Slack, docs) to sanitized HTML for LiveView.

  ## Sanitizer choice

  We do **allowlist filtering of the Earmark AST** (`Earmark.Parser.as_ast/1`)
  followed by our own escaping renderer, instead of adding an HTML
  sanitization dependency (e.g. `html_sanitize_ex`). Rationale:

    * Earmark 1.4.48 (already a dep) vendors a full parser that returns raw
      HTML blocks as tagged AST nodes (`%{verbatim: true}`), so we never have
      to re-parse HTML with regexes — we filter structured nodes.
    * Zero new dependencies means no network access during `deps.get` in the
      Nix sandbox and no second HTML parser (mochiweb) to keep patched.
    * Allowlist semantics: anything not explicitly permitted is stripped, so
      new attack vectors (new tags/attributes) are rejected by default.

  ## Policy

    * Allowed tags: headings, paragraphs, lists, tables, `code`/`pre`,
      blockquotes, `em`/`strong`/`del`, `hr`/`br`, and anchors.
    * Anchors: `href` must start with `https://` or `mailto:`
      (case-insensitive, after trimming); `rel="noopener noreferrer"` and
      `target="_blank"` are forced; all other attributes are dropped.
      `javascript:`, `data:`, `vbscript:` and every other scheme are rejected
      — the offending anchor is unwrapped to its plain text content.
    * `script`, `style`, `iframe`, `object`, `embed`, `form` (and other
      active/embedding tags) are dropped **including their content**.
    * All event-handler attributes (`onclick`, `onerror`, …) — and in fact
      all attributes except the tiny per-tag allowlist below — are stripped.
    * Images are **dropped entirely** (both markdown `![]()` and raw
      `<img>`): the judge view has no legitimate image use today, and
      dropping removes the whole `onerror`/tracking-pixel surface. Revisit
      with an `https://`-only allowlist if images become a real need.
    * Unknown-but-harmless tags (`div`, `span`, …) are unwrapped: their
      (sanitized, escaped) children are kept, the tag itself is not.
    * All text — including the contents of raw/verbatim HTML blocks — is
      HTML-escaped on output.
  """

  @allowed_tags ~w(h1 h2 h3 h4 h5 h6 p ul ol li table thead tbody tr th td
                   code pre blockquote em strong del hr br a)

  @void_tags ~w(hr br)

  # Dropped together with their entire content.
  @drop_with_content ~w(script style iframe object embed form svg math
                        link meta base title noscript template textarea
                        select button input frame frameset applet)

  @doc """
  Renders untrusted markdown to sanitized, HTML-safe iodata.

  Returns a `Phoenix.HTML.safe()` tuple, so call sites can interpolate the
  result directly in HEEx (`{SafeMarkdown.render(body)}`) without `raw/1`.
  """
  @spec render(String.t() | nil) :: Phoenix.HTML.safe()
  def render(nil), do: {:safe, ""}

  def render(markdown) when is_binary(markdown) do
    ast =
      case Earmark.Parser.as_ast(markdown) do
        {:ok, ast, _messages} -> ast
        {:error, ast, _messages} -> ast
      end

    {:safe, ast |> sanitize_nodes() |> Enum.map(&node_to_iodata/1)}
  end

  @doc """
  Defense-in-depth check for externally sourced link URLs (e.g. work-order
  link chips): only plain `https://` URLs are considered safe to href.
  """
  @spec safe_link?(term()) :: boolean()
  def safe_link?(url) when is_binary(url), do: String.starts_with?(url, "https://")
  def safe_link?(_), do: false

  # -- AST sanitization -----------------------------------------------------

  defp sanitize_nodes(nodes) when is_list(nodes), do: Enum.flat_map(nodes, &sanitize_node/1)

  defp sanitize_node(text) when is_binary(text), do: [text]

  defp sanitize_node({tag, attrs, children, _meta}) when is_binary(tag) do
    tag = String.downcase(tag)

    cond do
      tag in @drop_with_content or tag == "img" ->
        []

      tag == "a" ->
        case safe_href(attrs) do
          {:ok, href} ->
            forced = [
              {"href", href},
              {"rel", "noopener noreferrer"},
              {"target", "_blank"}
            ]

            [{"a", forced, sanitize_nodes(children)}]

          :error ->
            sanitize_nodes(children)
        end

      tag in @allowed_tags ->
        [{tag, keep_attrs(tag, attrs), sanitize_nodes(children)}]

      true ->
        # Unknown but non-dangerous tag: unwrap, keep sanitized children.
        sanitize_nodes(children)
    end
  end

  # Comments and anything else non-standard are dropped.
  defp sanitize_node(_), do: []

  defp safe_href(attrs) do
    case List.keyfind(attrs, "href", 0) do
      {"href", url} when is_binary(url) ->
        normalized = url |> String.trim() |> String.downcase()

        if String.starts_with?(normalized, "https://") or
             String.starts_with?(normalized, "mailto:") do
          {:ok, String.trim(url)}
        else
          :error
        end

      _ ->
        :error
    end
  end

  # Per-tag attribute allowlist. Everything else (event handlers, style,
  # class, id, data-*) is stripped.
  defp keep_attrs("code", attrs) do
    case List.keyfind(attrs, "class", 0) do
      {"class", class} when is_binary(class) ->
        if class =~ ~r/\A[a-zA-Z0-9_+\-]{1,40}\z/, do: [{"class", class}], else: []

      _ ->
        []
    end
  end

  defp keep_attrs(cell, attrs) when cell in ["th", "td"] do
    case List.keyfind(attrs, "style", 0) do
      {"style", style} when is_binary(style) ->
        if style =~ ~r/\Atext-align:\s*(left|right|center);?\z/,
          do: [{"style", style}],
          else: []

      _ ->
        []
    end
  end

  defp keep_attrs(_tag, _attrs), do: []

  # -- HTML rendering (escapes all text and attribute values) ----------------

  defp node_to_iodata(text) when is_binary(text), do: Plug.HTML.html_escape_to_iodata(text)

  defp node_to_iodata({tag, attrs, _children}) when tag in @void_tags do
    ["<", tag, attrs_to_iodata(attrs), "/>"]
  end

  defp node_to_iodata({tag, attrs, children}) do
    [
      "<",
      tag,
      attrs_to_iodata(attrs),
      ">",
      Enum.map(children, &node_to_iodata/1),
      "</",
      tag,
      ">"
    ]
  end

  defp attrs_to_iodata(attrs) do
    Enum.map(attrs, fn {name, value} ->
      [" ", name, "=\"", Plug.HTML.html_escape_to_iodata(value), "\""]
    end)
  end
end

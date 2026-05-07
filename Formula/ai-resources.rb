# frozen_string_literal: true
# Tap: same repo as the kit. Use: brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git
# Tag vX.Y.Z must exist on github.com/wildbitca/ai-resources (see CHANGELOG.md).
# Private repo: export HOMEBREW_GITHUB_API_TOKEN=ghp_... so Homebrew can clone over HTTPS.
class AiResources < Formula
  include Language::Python::Virtualenv

  desc "Multi-model AI agent kit: skills, workflows, orchestration, LiteLLM (via pipx, no Docker)"
  homepage "https://github.com/wildbitca/ai-resources"
  url "https://github.com/wildbitca/ai-resources.git",
      using: :git,
      tag: "v1.1.0"
  version "1.1.0"

  head "https://github.com/wildbitca/ai-resources.git", branch: "main"

  depends_on "python@3.12"
  depends_on "pipx"
  depends_on "gentleman-programming/tap/engram"

  # Python deps for the wizard UI + LiteLLM HTTP client + YAML parsing.
  # Pinned to known-good versions; bump in lockstep with the kit.
  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.9.4.tar.gz"
    sha256 "439594978a49a09530cff7ebc4b5c7103ef57baf48d5ea3184f21d9a2befa098"
  end

  resource "questionary" do
    url "https://files.pythonhosted.org/packages/source/q/questionary/questionary-2.1.0.tar.gz"
    sha256 "6d7c3a6f6b9c7a2e0e6ddebc5b7a6fcdb14f7ef7c5ef80fc2c0c2d1e5b0e9a0e"
  end

  resource "prompt_toolkit" do
    url "https://files.pythonhosted.org/packages/source/p/prompt_toolkit/prompt_toolkit-3.0.48.tar.gz"
    sha256 "d6623ab0477a80df74e646bdbc93621143f5caf104206aa29294d53de1a03d90"
  end

  resource "wcwidth" do
    url "https://files.pythonhosted.org/packages/source/w/wcwidth/wcwidth-0.2.13.tar.gz"
    sha256 "72ea0c06399eb286d978fdedb6923a9eb47e1c486ce63e9b4e64fc18303972b5"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/source/m/markdown-it-py/markdown-it-py-3.0.0.tar.gz"
    sha256 "e3f60a94fa066dc52ec76661e37c851cb232d92f9886b15cb560aaada2df8feb"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/source/m/mdurl/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "Pygments" do
    url "https://files.pythonhosted.org/packages/source/p/pygments/pygments-2.18.0.tar.gz"
    sha256 "786ff802f32e91311bff3889f6e9a86e81505fe99f2735bb6d60ae0c5004f199"
  end

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/source/P/PyYAML/PyYAML-6.0.2.tar.gz"
    sha256 "d584d9ec91ad65861cc08d42e834324ef890a082e591037abe114850ff7bbc3e"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.28.1.tar.gz"
    sha256 "75e98c5f16b0f35b567856f597f06ff2270a374470a5c2392242528e3e3e42fc"
  end

  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/source/h/httpcore/httpcore-1.0.7.tar.gz"
    sha256 "8551cb62a169ec7162ac7be8d4817d561f60e08eaa485234898414bb5a8a0b4c"
  end

  resource "h11" do
    url "https://files.pythonhosted.org/packages/source/h/h11/h11-0.14.0.tar.gz"
    sha256 "8f19fbbe99e72420ff35c00b27a34cb9937e902a8b810e2c88300c6f0a3b699d"
  end

  resource "anyio" do
    url "https://files.pythonhosted.org/packages/source/a/anyio/anyio-4.7.0.tar.gz"
    sha256 "2f834749c602966b7d456a7567cafcb309f96482b5081d14ac93ccd457f9dd48"
  end

  resource "sniffio" do
    url "https://files.pythonhosted.org/packages/source/s/sniffio/sniffio-1.3.1.tar.gz"
    sha256 "f4324edc670a0f49750a81b895f35c3adb843cca46f0530f79fc1babb23789dc"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/source/i/idna/idna-3.10.tar.gz"
    sha256 "12f65c9b470abda6dc35cf8e63cc574b1c52b11df2c86030af0ac09b01b13ea9"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/source/c/certifi/certifi-2024.12.14.tar.gz"
    sha256 "b650d30f370c2b724812bee08008be0c4163b163ddaec3f2546c1caf65f191db"
  end

  resource "typing_extensions" do
    url "https://files.pythonhosted.org/packages/source/t/typing_extensions/typing_extensions-4.12.2.tar.gz"
    sha256 "1a7ead55c7e559dd4dee8856e3a88b41225abfe1ce8df57b7c13915fe121ffb8"
  end

  def install
    libexec.install Dir["*"]
    venv = virtualenv_create(libexec/"venv", Formula["python@3.12"].opt_bin/"python3.12")
    venv.pip_install resources

    (bin/"ai-resources").write <<~EOS
      #!/usr/bin/env bash
      set -e
      export AGENT_KIT="#{libexec}"
      exec "#{libexec}/venv/bin/python" "#{libexec}/scripts/kit.py" "$@"
    EOS
  end

  test do
    out = shell_output("#{bin}/ai-resources --help")
    assert_match "generate", out
    assert_match "setup", out
    assert_match "doctor", out
    assert_match "daemon", out
  end
end

# frozen_string_literal: true
# Tap: same repo as the kit. Use: brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git
# (without URL, Homebrew looks for github.com/wildbitca/homebrew-ai-resources).
# Tag vX.Y.Z must exist on github.com/wildbitca/ai-resources (see CHANGELOG.md).
# Private repo: export HOMEBREW_GITHUB_API_TOKEN=ghp_... so Homebrew can clone over HTTPS.
class AiResources < Formula
  desc "Agent rules, skills, workflows, and kit CLI (generate, setup)"
  homepage "https://github.com/wildbitca/ai-resources"
  url "https://github.com/wildbitca/ai-resources.git",
      using: :git,
      tag: "v0.7.0"
  version "0.7.0"

  head "https://github.com/wildbitca/ai-resources.git", branch: "main"

  depends_on "python@3.12"
  depends_on "gentleman-programming/tap/engram"

  def install
    libexec.install Dir["*"]
    py_bin = Formula["python@3.12"].opt_bin

    (bin/"ai-resources").write <<~EOS
      #!/usr/bin/env bash
      set -e
      export AGENT_KIT="#{libexec}"
      export PATH="#{py_bin}:$PATH"
      for _py in python3 python3.12; do
        if command -v "${_py}" >/dev/null 2>&1; then
          exec "${_py}" "#{libexec}/scripts/kit.py" "$@"
        fi
      done
      echo "ai-resources: python3 not found (try: brew reinstall python@3.12)" >&2
      exit 1
    EOS
  end

  test do
    out = shell_output("#{bin}/ai-resources --help")
    assert_match "generate", out
    assert_match "setup", out
  end
end

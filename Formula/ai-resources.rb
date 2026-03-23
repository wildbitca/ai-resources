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
      tag: "v0.1.0"
  version "0.1.0"
  revision 0

  head "https://github.com/wildbitca/ai-resources.git", branch: "main"

  depends_on "python@3.12"

  def install
    libexec.install Dir["*"]

    (bin/"ai-resources").write <<~EOS
      #!/usr/bin/env bash
      export AGENT_KIT="#{libexec}"
      exec "#{Formula["python@3.12"].opt_bin}/python3" "#{libexec}/scripts/kit.py" "$@"
    EOS
  end

  test do
    out = shell_output("#{bin}/ai-resources --help")
    assert_match "generate", out
    assert_match "setup", out
  end
end

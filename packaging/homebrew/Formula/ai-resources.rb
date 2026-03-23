# frozen_string_literal: true
# Package version must match an existing Git tag vX.Y.Z on github.com/wildbitca/ai-resources (see CHANGELOG.md).
class AiResources < Formula
  desc "Agent rules, skills, workflows, and kit CLI (generate, setup)"
  homepage "https://github.com/wildbitca/ai-resources"
  url "https://github.com/wildbitca/ai-resources/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "22e641e7317c0f9262aefd2710041116a30b8d4965202dfd589d1314cf339d99"
  version "0.1.0"
  revision 0

  head "https://github.com/wildbitca/ai-resources.git", branch: "main"

  # Pin Python for reproducible installs; `brew install python@3.12` if missing.
  depends_on "python@3.12"

  def install
    # GitHub archive unpacks to ai-resources-<version>/
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

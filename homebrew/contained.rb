# typed: false
# frozen_string_literal: true

class Contained < Formula
  desc "contAIned — take back control of your coding agent"
  homepage "https://github.com/lab-v2/contAIned"
  version "0.1.0"
  license "MIT"

  on_macos do
    on_arm do
      url "https://github.com/lab-v2/contAIned/releases/download/v#{version}/contained_#{version}_darwin_arm64"
      sha256 "REPLACE_WITH_SHA256_darwin_arm64"

      def install
        bin.install "contained_#{version}_darwin_arm64" => "contained"
      end
    end

    on_intel do
      url "https://github.com/lab-v2/contAIned/releases/download/v#{version}/contained_#{version}_darwin_amd64"
      sha256 "REPLACE_WITH_SHA256_darwin_amd64"

      def install
        bin.install "contained_#{version}_darwin_amd64" => "contained"
      end
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/lab-v2/contAIned/releases/download/v#{version}/contained_#{version}_linux_arm64"
      sha256 "REPLACE_WITH_SHA256_linux_arm64"

      def install
        bin.install "contained_#{version}_linux_arm64" => "contained"
      end
    end

    on_intel do
      url "https://github.com/lab-v2/contAIned/releases/download/v#{version}/contained_#{version}_linux_amd64"
      sha256 "REPLACE_WITH_SHA256_linux_amd64"

      def install
        bin.install "contained_#{version}_linux_amd64" => "contained"
      end
    end
  end

  test do
    assert_match "contAIned", shell_output("#{bin}/contained --version")
  end
end

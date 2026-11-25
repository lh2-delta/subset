#!/bin/sh
# Install the Unity Version Control (Plastic SCM) command-line client `cm` in the image.
# The vendor's apt repo answers 403 for InRelease, which apt treats as fatal, so the chain is checked by hand:
# Release signature (vendor key) -> Packages sha256 (in Release) -> .deb sha256 (in Packages). amd64 only.
set -eu
BASE=https://d26z97tczqnlef.cloudfront.net/linux-packages/stable/debian
KEY=https://www.plasticscm.com/plasticrepo/stable/debian/Release.key
PKGS="${PLASTIC_PKGS:-plasticscm-client-core}"
work=$(mktemp -d); cd "$work"
curl -fsSL "$KEY" | gpg --dearmor > key.gpg
curl -fsSL "$BASE/Release" -o Release
curl -fsSL "$BASE/Release.gpg" -o Release.gpg
gpgv --keyring ./key.gpg Release.gpg Release
curl -fsSL "$BASE/Packages" -o Packages
want=$(awk '/^SHA256:/{f=1;next} /^[A-Za-z]/{f=0} f && $3=="Packages"{print $1}' Release)
echo "$want  Packages" | sha256sum -c -
for pkg in $PKGS; do
  # newest version of the package: the last stanza for it in Packages
  stanza=$(awk -v p="$pkg" 'BEGIN{RS="";FS="\n"} {for(i=1;i<=NF;i++) if($i=="Package: " p){s=$0}} END{print s}' Packages)
  file=$(printf '%s\n' "$stanza" | sed -n 's/^Filename: \.\///p')
  sum=$(printf '%s\n' "$stanza" | sed -n 's/^SHA256: //p')
  deps=$(printf '%s\n' "$stanza" | sed -n 's/^Depends: //p')
  curl -fsSL --retry 5 --retry-all-errors "$BASE/$file" -o "$pkg.deb"
  echo "$sum  $pkg.deb" | sha256sum -c -
  echo "$pkg: $file"
done
apt-get update -qq
apt-get install -y --no-install-recommends ./*.deb
cd /; rm -rf "$work"
cm version

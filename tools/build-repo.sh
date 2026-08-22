#!/bin/bash
#
# Build a signed apt/dnf repository under repo/ from 3proxy release assets.
#
# Two channels are published, matching the docker tags:
#
#   lts      releases reachable from the 0.9 branch
#   current  releases reachable from master
#
# A release that predates the branch point belongs to both.  Channel membership
# is derived from git ancestry, so no manual list has to be maintained.
#
# Only releases that ship a GPG-signed SHA256SUMS file are included: every
# package is checksum-verified against a signature made by a published 3proxy
# key before it enters the repository.  Packages are re-signed with the current
# release key so that rpm's gpgcheck works with a single imported key.
#
# Existing packages are never re-downloaded or re-signed, so repeated runs are
# idempotent and only new releases produce a commit.

set -euo pipefail

SRC_REPO="${SRC_REPO:-3proxy/3proxy}"
ROOT="${ROOT:-repo}"
COMPONENT="${COMPONENT:-main}"
MAX_RELEASES="${MAX_RELEASES:-50}"
ORIGIN="${ORIGIN:-3proxy}"
CHANNELS="${CHANNELS:-lts:0.9 current:master}"

: "${GPG_KEYID:?GPG_KEYID must be set}"

WORK="$(mktemp -d)"
PASSFILE="$WORK/pass"
trap 'rm -rf "$WORK"' EXIT
umask 077
printf '%s' "${GPG_PASSPHRASE:-}" > "$PASSFILE"
umask 022

DEBROOT="$ROOT/deb"
RPMROOT="$ROOT/rpm"
mkdir -p "$DEBROOT" "$RPMROOT"

gpgsign() {
	gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASSFILE" \
	    -u "$GPG_KEYID" "$@"
}

# Verification key, fetched from the source repository.
for k in 3proxy-release-key.asc; do
	if curl -fsSL "https://raw.githubusercontent.com/$SRC_REPO/master/$k" -o "$WORK/$k" 2>/dev/null; then
		gpg --batch --quiet --import "$WORK/$k" 2>/dev/null || true
	fi
done

GPGBIN="$(command -v gpg)"
{
	echo "%_gpg_name $GPG_KEYID"
	echo "%__gpg $GPGBIN"
	echo "%__gpg_sign_cmd %{__gpg} gpg --batch --no-armor --pinentry-mode loopback --passphrase-file $PASSFILE --no-secmem-warning --digest-algo sha256 -u \"%{_gpg_name}\" -sbo %{__signature_filename} %{__plaintext_filename}"
} > "$HOME/.rpmmacros"

# Branch heads, for deciding which channel a tag belongs to.
git clone --quiet --bare --filter=blob:none "https://github.com/$SRC_REPO" "$WORK/src"

channel_branch() {
	for spec in $CHANNELS; do
		[ "${spec%%:*}" = "$1" ] && { echo "${spec#*:}"; return; }
	done
}

added=0
touched=""

tags=$(gh release list -R "$SRC_REPO" --limit "$MAX_RELEASES" \
	--json tagName,isPrerelease,isDraft \
	--jq '.[] | select(.isPrerelease == false and .isDraft == false) | .tagName')

for tag in $tags; do
	assets=$(gh release view "$tag" -R "$SRC_REPO" --json assets --jq '.assets[].name')
	pkgs=$(printf '%s\n' "$assets" | grep -E '\.(rpm|deb)$' || true)
	[ -n "$pkgs" ] || continue

	if ! printf '%s\n' "$assets" | grep -q '^SHA256SUMS-.*\.asc$'; then
		echo "skip $tag: no signed checksums, cannot verify"
		continue
	fi

	# Which channels contain this tag?
	chans=""
	for spec in $CHANNELS; do
		chan="${spec%%:*}"
		branch="${spec#*:}"
		git -C "$WORK/src" rev-parse --verify --quiet "refs/heads/$branch" >/dev/null || continue
		if git -C "$WORK/src" merge-base --is-ancestor \
			"refs/tags/$tag^{commit}" "refs/heads/$branch" 2>/dev/null; then
			chans="$chans $chan"
		fi
	done
	[ -n "$chans" ] || { echo "skip $tag: not on any published branch"; continue; }

	# Anything missing in any of its channels?
	missing=0
	for chan in $chans; do
		for p in $pkgs; do
			case "$p" in
			*.deb) [ -n "$(find "$DEBROOT/pool/$chan" -name "$p" -type f -print -quit 2>/dev/null)" ] || missing=1 ;;
			*.rpm) [ -n "$(find "$RPMROOT/$chan" -name "$p" -type f -print -quit 2>/dev/null)" ] || missing=1 ;;
			esac
		done
	done
	[ "$missing" = 1 ] || continue

	echo "fetching $tag (channels:$chans)"
	d="$WORK/$tag"
	mkdir -p "$d"
	gh release download "$tag" -R "$SRC_REPO" -D "$d" \
		-p '*.rpm' -p '*.deb' -p 'SHA256SUMS-*'

	# Every checksum file must carry a good signature from a published key.
	# Their union is then the authenticated list of hashes; each downloaded
	# package must appear in it and match.  Checksum files covering artifacts
	# we do not mirror (the Windows zips) contribute nothing and are harmless.
	ok=1
	: > "$d/.sums"
	for s in "$d"/SHA256SUMS-*; do
		case "$s" in *.asc) continue ;; esac
		[ -e "$s.asc" ] || { echo "  ${s##*/}: no signature"; ok=0; break; }
		if ! gpg --batch --verify "$s.asc" "$s" >/dev/null 2>&1; then
			echo "  ${s##*/}: bad signature"
			ok=0
			break
		fi
		cat "$s" >> "$d/.sums"
	done
	if [ "$ok" = 1 ]; then
		for f in "$d"/*.rpm "$d"/*.deb; do
			[ -e "$f" ] || continue
			b="${f##*/}"
			want=$(awk -v b="$b" '{ n = $2; sub(/^\*/, "", n); if (n == b) { print $1; exit } }' "$d/.sums")
			if [ -z "$want" ]; then
				echo "  $b: not covered by any signed checksum file"
				ok=0
				break
			fi
			got=$(sha256sum "$f" | cut -d' ' -f1)
			if [ "$want" != "$got" ]; then
				echo "  $b: checksum mismatch"
				ok=0
				break
			fi
		done
	fi
	[ "$ok" = 1 ] || { echo "  $tag rejected"; continue; }

	for chan in $chans; do
		for f in "$d"/*.deb; do
			[ -e "$f" ] || continue
			b="${f##*/}"
			# apt-ftparchive's --arch filter keys off the Debian
			# name_version_arch.deb convention, which these packages do
			# not follow, so keep one pool directory per architecture and
			# index each separately.
			arch=$(dpkg-deb -f "$f" Architecture 2>/dev/null)
			[ -n "$arch" ] || { echo "  ! cannot read arch of $b"; continue; }
			pool="$DEBROOT/pool/$chan/$COMPONENT/3/3proxy/$arch"
			mkdir -p "$pool"
			[ -e "$pool/$b" ] && continue
			cp "$f" "$pool/$b"
			echo "  + deb $chan/$arch/$b"
			added=1
			case "$touched" in *"deb:$chan "*) ;; *) touched="$touched deb:$chan " ;; esac
		done
		for f in "$d"/*.rpm; do
			[ -e "$f" ] || continue
			b="${f##*/}"
			arch=$(rpm -qp --qf '%{ARCH}' "$f" 2>/dev/null)
			[ -n "$arch" ] || { echo "  ! cannot read arch of $b"; continue; }
			# Packages are published per Enterprise Linux major version,
			# so that $releasever selects the right one.  Anything without
			# a dist tag was not built against an EL base and would not
			# install on one.
			dist=$(rpm -qp --qf '%{RELEASE}' "$f" 2>/dev/null | sed 's/^[0-9]*\.//')
			case "$dist" in
			el*) ;;
			*)   echo "  ! $b has no EL dist tag, skipping"; continue ;;
			esac
			[ -e "$RPMROOT/$chan/$dist/$arch/$b" ] && continue
			mkdir -p "$RPMROOT/$chan/$dist/$arch"
			cp "$f" "$RPMROOT/$chan/$dist/$arch/$b"
			rpm --delsign "$RPMROOT/$chan/$dist/$arch/$b" >/dev/null 2>&1 || true
			rpm --addsign "$RPMROOT/$chan/$dist/$arch/$b" >/dev/null
			echo "  + rpm $chan/$dist/$arch/$b"
			added=1
			case "$touched" in *"rpm:$chan/$dist/$arch "*) ;; *) touched="$touched rpm:$chan/$dist/$arch " ;; esac
		done
	done
done

if [ "$added" = 0 ]; then
	echo "no new packages, repository metadata left untouched"
	exit 0
fi

for t in $touched; do
	case "$t" in
	rpm:*)
		d="$RPMROOT/${t#rpm:}"
		echo "rpm metadata: ${t#rpm:}"
		createrepo_c --quiet --update "$d"
		rm -f "$d/repodata/repomd.xml.asc"
		gpgsign --detach-sign --armor "$d/repodata/repomd.xml"
		;;
	deb:*)
		chan="${t#deb:}"
		echo "deb metadata: $chan"
		(
			cd "$DEBROOT"
			base="pool/$chan/$COMPONENT/3/3proxy"
			archs=$(ls "$base" 2>/dev/null | sort -u | tr '\n' ' ')
			rm -rf "dists/$chan"
			for a in $archs; do
				mkdir -p "dists/$chan/$COMPONENT/binary-$a"
				apt-ftparchive packages "$base/$a" \
					> "dists/$chan/$COMPONENT/binary-$a/Packages"
				gzip -9nc "dists/$chan/$COMPONENT/binary-$a/Packages" \
					> "dists/$chan/$COMPONENT/binary-$a/Packages.gz"
			done
			apt-ftparchive \
				-o APT::FTPArchive::Release::Origin="$ORIGIN" \
				-o APT::FTPArchive::Release::Label="$ORIGIN" \
				-o APT::FTPArchive::Release::Suite="$chan" \
				-o APT::FTPArchive::Release::Codename="$chan" \
				-o APT::FTPArchive::Release::Components="$COMPONENT" \
				-o APT::FTPArchive::Release::Architectures="$archs" \
				release "dists/$chan" > "dists/$chan/Release"
		)
		gpgsign --clearsign -o "$DEBROOT/dists/$chan/InRelease" "$DEBROOT/dists/$chan/Release"
		gpgsign --detach-sign --armor -o "$DEBROOT/dists/$chan/Release.gpg" "$DEBROOT/dists/$chan/Release"
		;;
	esac
done

cp "$WORK/3proxy-release-key.asc" "$ROOT/3proxy-release-key.asc" 2>/dev/null || true

echo "done"

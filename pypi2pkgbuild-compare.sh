#!/bin/bash

# Compare dependencies between installed and automatic packages.
# First run e.g.
#     pypi2pkgbuild.py $(pacman -Qm | grep python- | grep -v 00 | grep -v git | cut -d' ' -f1 | cut -d- -f2-)
# and run this script from the packages-containing folder.

for pkgname in *; do
    echo "$(tput bold)$pkgname$(tput sgr0)"
    found="$(
        grep '\bdepends' "$pkgname/.SRCINFO" |
        cut -d' ' -f3 | grep -v '^python$' | sort)"
    info="$(pacman -Qi "$pkgname")"
    depends="$(
        grep -Po '(?<=Depends On).*' <<<"$info" |
        cut -d: -f2 | tr ' ' '\n' | grep -v '^python$')"
    optdepends="$(
        grep -Pzo '(?<=Optional Deps)(.|\n)*?\n(?=\S)' <<<"$info" |
        tr -d '\0' | sed 's/^[ :]*//' | grep -av '^None$')"
    fulldepends="$(grep -o '^[[:alnum:]-]\+' <<<$"$depends\n$optdepends" | sort)"
    c1="$(comm -23 <(echo "$found") <(echo "$fulldepends"))"
    c2="$(comm -13 <(echo "$found") <(echo "$fulldepends"))"
    c3="$(comm -12 <(echo "$found") <(echo "$fulldepends"))"
    if [[ "$c3" ]]; then
        echo "$(tput smul)Common$(tput sgr0)"
        echo "$c3"
    fi
    if [[ "$c2" ]]; then
        echo "$(tput smul)Only in installed package$(tput sgr0)"
        echo "$c2"
    fi
    if [[ "$c1" ]]; then
        echo "$(tput smul)Only in automatic package$(tput sgr0)"
        echo "$c1"
    fi
    printf '\n------\n'
done

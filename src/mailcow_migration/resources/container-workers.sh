set -eu
for p in /proc/[0-9]*; do
    if [ -r "$p/comm" ]; then
        read -r name < "$p/comm" || exit
        case " $1 " in *" $name "*) printf '%s\n' "${p##*/}";; esac
    fi
done

# bash completion for bag-drive. Installed to
# /usr/share/bash-completion/completions/bag-drive by install.sh.
# Drive names come from "bag-drive status --json", which needs no sudo.

_bag_drive_names() {
    bag-drive status --json 2>/dev/null | python3 -c '
import json, sys
for d in json.load(sys.stdin).get("drives", []):
    print(d["name"])' 2>/dev/null
}

_bag_drive() {
    local cur="${COMP_WORDS[COMP_CWORD]}" cmd="" i
    for ((i = 1; i < COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            -*) ;;
            *) cmd="${COMP_WORDS[i]}"; break ;;
        esac
    done
    case "$cmd" in
        "")
            COMPREPLY=($(compgen -W "status eject unmount --help" -- "$cur")) ;;
        status)
            COMPREPLY=($(compgen -W "--json" -- "$cur")) ;;
        eject|unmount|umount)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--all" -- "$cur"))
            else
                local IFS=$'\n' names=() n w seen
                for n in $(_bag_drive_names); do
                    seen=
                    for w in "${COMP_WORDS[@]:i+1:COMP_CWORD-i-1}"; do
                        [[ "$w" == "$n" ]] && seen=1
                    done
                    [[ -z "$seen" ]] && names+=("$n")
                done
                COMPREPLY=($(compgen -W "${names[*]}" -- "$cur"))
                # Escape names with spaces so each stays one word.
                for i in "${!COMPREPLY[@]}"; do
                    COMPREPLY[i]=$(printf '%q' "${COMPREPLY[i]}")
                done
            fi ;;
    esac
}
complete -F _bag_drive bag-drive

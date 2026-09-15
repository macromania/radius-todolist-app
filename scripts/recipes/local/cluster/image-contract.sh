#!/bin/sh

check_prefix() {
    case "$1" in ''|*[!a-z0-9-]*|-*|*-) return 1 ;; esac
    case "$1" in [a-z]*) ;; *) return 1 ;; esac
    [ "${#1}" -le 25 ]
}

check_image() {
    case "$2" in ''|*[!a-zA-Z0-9./_:@-]*) return 1 ;; esac
    case "$2" in
        localhost/"$1"-api:*|localhost/"$1"-provisioner:*|localhost/"$1"-operator:*)
            tag=${2##*:}
            case "$tag" in *[!a-f0-9]*) return 1 ;; esac
            [ "${#tag}" -eq 40 ] ;;
        ghcr.io/radius-project/*:*|docker.io/library/*@sha256:*|\
        docker.io/envoyproxy/*@sha256:*|kindest/node:*@sha256:*) return 0 ;;
        *) return 1 ;;
    esac
}

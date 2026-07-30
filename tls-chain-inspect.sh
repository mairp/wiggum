#!/usr/bin/env bash
#
# Inspect and split a local PEM certificate bundle into its available parts.

set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: tls-chain-inspect.sh INPUT_FILE [OUTPUT_DIRECTORY]

Extract the available TLS material from a saved PEM bundle:
  private-key.pem          Private key, when present
  server-certificate.pem   Leaf/server certificate
  issuing-ca.pem           Issuing/intermediate CA certificate(s)
  root-ca.pem              Self-signed root CA certificate(s)

Missing sections are reported and skipped; the remaining sections are still
written. A single DER-encoded certificate is also accepted, but DER cannot
contain a private key or a complete certificate chain.

If OUTPUT_DIRECTORY is omitted, files are written below:
  ./<input-name>-extracted/

Examples:
  ./tls-chain-inspect.sh fullchain-with-key.pem
  ./tls-chain-inspect.sh fullchain.pem ./certificate-parts
  ./tls-chain-inspect.sh server.cer
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

warn() {
  printf 'Warning: %s\n' "$*" >&2
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

[[ $# -ge 1 && $# -le 2 ]] || {
  usage >&2
  exit 2
}

for command_name in openssl awk grep mktemp; do
  command -v "$command_name" >/dev/null 2>&1 ||
    die "$command_name is required but was not found in PATH"
done

input_file=$1
[[ -f $input_file ]] || die "file not found: $input_file"
[[ -r $input_file ]] || die "file is not readable: $input_file"

input_name=${input_file##*/}
input_stem=${input_name%.*}
output_dir=${2:-"./${input_stem}-extracted"}

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/tls-chain-extract.XXXXXXXX")
trap 'rm -rf -- "$work_dir"' EXIT HUP INT TERM

cert_prefix="$work_dir/cert-"
extracted_key="$work_dir/private-key.pem"

if grep -q -- '-----BEGIN CERTIFICATE-----' "$input_file"; then
  # Extract every certificate while preserving its order in the source file.
  awk -v prefix="$cert_prefix" '
    /-----BEGIN CERTIFICATE-----/ {
      certificate++
      output = sprintf("%s%03d.pem", prefix, certificate)
      copying = 1
    }
    copying {
      print > output
    }
    /-----END CERTIFICATE-----/ {
      close(output)
      copying = 0
    }
  ' "$input_file"

  # Copy any traditional, PKCS#8, or encrypted PEM private-key block. This
  # deliberately does not decrypt or otherwise alter sensitive key material.
  awk -v output="$extracted_key" '
    /^-----BEGIN .*PRIVATE KEY-----$/ {
      copying = 1
      found = 1
    }
    copying {
      print > output
    }
    /^-----END .*PRIVATE KEY-----$/ {
      copying = 0
    }
    END {
      if (found) {
        close(output)
      }
    }
  ' "$input_file"
else
  openssl x509 -inform DER -in "$input_file" \
    -out "$cert_prefix"001.pem 2>/dev/null ||
    die "unsupported input; expected a PEM bundle or DER X.509 certificate"
fi

shopt -s nullglob
certificates=("$cert_prefix"*.pem)
shopt -u nullglob

((${#certificates[@]} > 0)) ||
  die "no complete certificates were found in: $input_file"

for cert in "${certificates[@]}"; do
  openssl x509 -in "$cert" -noout >/dev/null 2>&1 ||
    die "the input contains an invalid or incomplete certificate block"
done

# Classify certificates using Basic Constraints and self-issued identity.
declare -a is_ca is_root subjects issuers
for index in "${!certificates[@]}"; do
  cert=${certificates[$index]}
  subjects[$index]=$(openssl x509 -in "$cert" -noout -subject -nameopt RFC2253)
  issuers[$index]=$(openssl x509 -in "$cert" -noout -issuer -nameopt RFC2253)

  if openssl x509 -in "$cert" -noout -text |
    grep -q -- 'CA:TRUE'; then
    is_ca[$index]=1
  else
    is_ca[$index]=0
  fi

  if [[ ${is_ca[$index]} -eq 1 &&
        ${subjects[$index]#subject=} == "${issuers[$index]#issuer=}" ]]; then
    is_root[$index]=1
  else
    is_root[$index]=0
  fi
done

# Prefer a non-CA certificate as the server/leaf certificate.
server_index=
for index in "${!certificates[@]}"; do
  if [[ ${is_ca[$index]} -eq 0 ]]; then
    server_index=$index
    break
  fi
done

# If Basic Constraints are absent, use the first non-root certificate.
if [[ -z $server_index ]]; then
  for index in "${!certificates[@]}"; do
    if [[ ${is_root[$index]} -eq 0 ]]; then
      server_index=$index
      break
    fi
  done
fi

mkdir -p -- "$output_dir"

declare -a output_files=(
  "$output_dir/private-key.pem"
  "$output_dir/server-certificate.pem"
  "$output_dir/issuing-ca.pem"
  "$output_dir/root-ca.pem"
)
for output_file in "${output_files[@]}"; do
  [[ ! -e $output_file ]] ||
    die "refusing to overwrite existing file: $output_file"
done

written=0

if [[ -s $extracted_key ]]; then
  cp -- "$extracted_key" "$output_dir/private-key.pem"
  chmod 600 "$output_dir/private-key.pem"
  printf 'Created: %s\n' "$output_dir/private-key.pem"
  ((written += 1))
else
  warn "no private key was found; continuing with available certificates"
fi

if [[ -n $server_index ]]; then
  cp -- "${certificates[$server_index]}" "$output_dir/server-certificate.pem"
  chmod 644 "$output_dir/server-certificate.pem"
  printf 'Created: %s\n' "$output_dir/server-certificate.pem"
  ((written += 1))
else
  warn "no server/leaf certificate could be identified"
fi

issuing_count=0
root_count=0
for index in "${!certificates[@]}"; do
  [[ $index != "$server_index" ]] || continue

  if [[ ${is_root[$index]} -eq 1 ]]; then
    cat "${certificates[$index]}" >>"$work_dir/root-ca.pem"
    ((root_count += 1))
  else
    cat "${certificates[$index]}" >>"$work_dir/issuing-ca.pem"
    ((issuing_count += 1))
  fi
done

if ((issuing_count > 0)); then
  cp -- "$work_dir/issuing-ca.pem" "$output_dir/issuing-ca.pem"
  chmod 644 "$output_dir/issuing-ca.pem"
  printf 'Created: %s (%d certificate(s))\n' \
    "$output_dir/issuing-ca.pem" "$issuing_count"
  ((written += 1))
else
  warn "no issuing/intermediate CA certificate was found"
fi

if ((root_count > 0)); then
  cp -- "$work_dir/root-ca.pem" "$output_dir/root-ca.pem"
  chmod 644 "$output_dir/root-ca.pem"
  printf 'Created: %s (%d certificate(s))\n' \
    "$output_dir/root-ca.pem" "$root_count"
  ((written += 1))
else
  warn "no self-signed root CA certificate was found"
fi

printf '\nExtracted %d of 4 section(s) into: %s\n' "$written" "$output_dir"

printf '\n%s\n' '=== Certificate classification ==='
for index in "${!certificates[@]}"; do
  if [[ $index == "$server_index" ]]; then
    role='server'
  elif [[ ${is_root[$index]} -eq 1 ]]; then
    role='root CA'
  else
    role='issuing CA'
  fi
  printf '[%d] %-10s %s\n' \
    "$((index + 1))" "$role" "${subjects[$index]}"
done

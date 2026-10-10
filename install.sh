#!/bin/bash
# kakao-mac 스킬 설치기.
# 이 저장소의 skill/kakao-mac 폴더를 사용자 스킬 폴더로 복사한다. 하는 일은 그것뿐이다.
# sudo와 네트워크를 쓰지 않고, 카카오톡 데이터에 손대지 않는다.
#
# 사용: bash install.sh [--force] [--skills-root <폴더>]
#   --force         이미 설치된 스킬을 ~/.kakao-mac/skill-backups/ 로 옮기고 새로 복사한다
#   --skills-root   스킬 폴더 위치 (기본 ~/.claude/skills)
set -euo pipefail

SKILL_NAME="kakao-mac"
SKILLS_ROOT="${HOME}/.claude/skills"
DATA_HOME="${KAKAO_MAC_HOME:-${HOME}/.kakao-mac}"
FORCE=0

usage() {
  echo "사용: bash install.sh [--force] [--skills-root <폴더>]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --force)
      FORCE=1
      ;;
    --skills-root)
      shift
      if [ $# -eq 0 ]; then
        echo "--skills-root 뒤에 폴더가 필요하다" >&2
        exit 2
      fi
      SKILLS_ROOT="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "알 수 없는 인자: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "MACOS_REQUIRED: 이 스킬은 macOS 전용이다." >&2
  exit 3
fi

if [ "$(id -u)" = "0" ]; then
  echo "ROOT_REFUSED: sudo 없이 평소 사용자로 실행한다." >&2
  exit 3
fi

SRC_ROOT="$(cd "$(dirname "$0")" && pwd -P)"
SRC="${SRC_ROOT}/skill/${SKILL_NAME}"

if [ ! -f "${SRC}/SKILL.md" ] || [ ! -f "${SRC}/scripts/kakao_mac.py" ]; then
  echo "SOURCE_MISSING: ${SRC} 에 스킬 파일이 없다. 저장소를 다시 받는다." >&2
  exit 4
fi

mkdir -p "${SKILLS_ROOT}"
SKILLS_ROOT="$(cd "${SKILLS_ROOT}" && pwd -P)"
DEST="${SKILLS_ROOT}/${SKILL_NAME}"

if [ "${SRC}" = "${DEST}" ]; then
  echo "SAME_PATH: 설치할 곳과 원본이 같은 폴더다. 저장소를 스킬 폴더 밖에 받는다." >&2
  exit 4
fi

if [ -e "${DEST}" ] || [ -L "${DEST}" ]; then
  if [ "${FORCE}" != "1" ]; then
    echo "Skill already exists: ${DEST}" >&2
    echo "프로그램만 새로 바꾸려면 --force 를 붙여 다시 실행한다. 키와 받아 둔 자료(${DATA_HOME})는 그대로 남는다." >&2
    exit 5
  fi
fi

# 옆 폴더에 먼저 복사하고 검사한 다음 바꿔 끼운다. 중간에 실패해도 기존 설치는 그대로 남는다.
STAGE="${SKILLS_ROOT}/.${SKILL_NAME}.installing.$$"
cleanup() {
  rm -rf "${STAGE}"
}
trap cleanup EXIT
rm -rf "${STAGE}"
cp -R "${SRC}" "${STAGE}"
cp "${SRC_ROOT}/LICENSE" "${SRC_ROOT}/THIRD_PARTY_NOTICES.md" "${STAGE}/"
find "${STAGE}" -type d -name "__pycache__" -prune -exec rm -rf {} +
chmod 755 "${STAGE}/scripts/kakao_mac.py" "${STAGE}/scripts/kakao_attachments.py"

# 복사된 스크립트가 이 맥의 python3에서 켜지는지만 본다. 카카오톡 데이터는 읽지 않는다.
if command -v python3 >/dev/null 2>&1; then
  if ! python3 "${STAGE}/scripts/kakao_mac.py" --help >/dev/null 2>&1; then
    echo "PYTHON_CHECK_FAILED: python3 로 스크립트를 켜지 못했다. python3 --version 을 확인한다(3.9 이상). 기존 설치는 바꾸지 않았다." >&2
    exit 6
  fi
else
  echo "주의: python3 를 찾지 못했다. 스크립트 실행에는 Python 3.9 이상이 필요하다." >&2
fi

BACKUP=""
if [ -e "${DEST}" ] || [ -L "${DEST}" ]; then
  BACKUP_ROOT="${DATA_HOME}/skill-backups"
  mkdir -p "${BACKUP_ROOT}"
  chmod 700 "${DATA_HOME}" "${BACKUP_ROOT}"
  BACKUP="${BACKUP_ROOT}/${SKILL_NAME}-$(date +%Y%m%d-%H%M%S)"
  mv "${DEST}" "${BACKUP}"
  echo "Previous skill preserved at: ${BACKUP}"
fi

if ! mv "${STAGE}" "${DEST}"; then
  if [ -n "${BACKUP}" ]; then
    mv "${BACKUP}" "${DEST}"
    echo "INSTALL_FAILED: 새 스킬을 놓지 못해 기존 설치를 되돌렸다." >&2
  else
    echo "INSTALL_FAILED: 새 스킬을 놓지 못했다." >&2
  fi
  exit 7
fi
trap - EXIT

echo "Installed: ${DEST}"

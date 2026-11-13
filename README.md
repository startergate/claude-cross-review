# claude-cross-review

Claude Code가 만든 변경을 사용자가 보기 전에 다른 코딩 에이전트(기본 Codex, 설정으로 Gemini 등 추가)가 먼저 검토하게 하는 Claude Code 플러그인입니다.

- **커밋 전**: 구현이 끝나면 Claude가 검토자 CLI들을 동시에 돌리고, 지적을 판단해 반영한 뒤 "고친 것 / 안 고친 것과 이유"를 붙여 보고합니다. 재확인까지 최대 2회.
- **PR 후**: Codex 공식 GitHub 연동이 단 리뷰를 Claude가 읽고, 고칠 건 고친 뒤 각 코멘트에 답글을 답니다.

검토자에게는 작업 목표, 기준 브랜치 대비 변경분, 이미 돌린 테스트·빌드 결과만 넘기고, 사용자와의 대화나 Claude의 판단은 넘기지 않습니다. 검토자는 저장소를 읽기 전용으로 직접 살펴봅니다.

## 설치

```bash
# Claude Code 안에서
/plugin marketplace add startergate/claude-cross-review
/plugin install cross-review@claude-cross-review
```

검토자 CLI도 설치하고 로그인해 둡니다.

```bash
npm install -g @openai/codex && codex login     # ChatGPT 계정으로 로그인
npm install -g @google/gemini-cli && gemini     # 선택: Gemini도 쓸 때
```

PR 단계는 chatgpt.com의 Codex 설정에서 저장소를 연결해 둡니다. 기본은 Claude가 PR을 연 뒤 `@codex review` 코멘트로 요청하는 방식이라, Claude가 연 PR만 검토됩니다. 모든 PR을 자동 리뷰하려면 Codex 설정에서 자동 리뷰를 켜고 `pr.request`를 `auto`로 바꿉니다.

## 사용

- 자동: 세션 시작 시 훅이 Claude에게 "보여주기 전에 cross-review 실행"을 알려 줍니다. Claude가 `gh pr create`로 PR을 열면 훅이 `@codex review` 요청 코멘트를 직접 달고, 리뷰 확인을 알려 줍니다. 사람이 GitHub 화면 등에서 직접 연 PR에는 달리지 않습니다.
- 직접: `/cross-review`, `/cross-review codex,gemini`, `/cross-review pr 12`

## 설정

나중 것이 앞의 것을 덮어씁니다: 플러그인 기본값(`plugins/cross-review/skills/cross-review/defaults.json`) → `~/.config/cross-review/config.json` → 저장소 루트 `.cross-review.json` → 실행 인자.

```json
{
  "reviewers": ["codex", "gemini"],
  "definitions": {
    "my-reviewer": {
      "command": "my-cli review --read-only < {prompt} > {output}"
    }
  }
}
```

| 키 | 설명 |
| --- | --- |
| `enabled` | `false`면 해당 저장소에서 끔 (비공개 코드를 외부 모델에 보내기 싫을 때) |
| `reviewers` | 함께 돌릴 검토자 이름 목록 |
| `definitions.<이름>.command` | 저장소 루트에서 실행할 명령. 검토 요청문이 stdin으로 들어감. `{repo}` `{prompt}` `{output}` 자리표시자 사용 가능, `{output}`에 안 쓰면 stdout이 결과 |
| `definitions.<이름>.check` | 실행 전 확인 명령 (설치·로그인 여부). 실패하면 그 검토자만 건너뜀 |
| `base` | 기준 브랜치. 없으면 develop → 기본 브랜치 순 |
| `maxRounds` | 커밋 전 검토 최대 횟수 (기본 2) |
| `timeoutSeconds` / `maxDiffBytes` | 검토자별 제한 시간 / 요청문에 넣을 diff 최대 크기 |
| `exclude` | diff에서 뺄 파일 패턴 (비밀 파일, lock 파일) |
| `language` | 검토 결과 언어 (기본 English) |
| `pr.request` | `comment`(기본, PR 연 뒤 `pr.requestComment`를 코멘트로 달아 요청) 또는 `auto`(연동이 모든 PR을 알아서 리뷰) |
| `pr.botLogins` | PR 리뷰를 읽을 봇 계정 |

## 주의

- 변경 코드와 검토자가 읽은 파일은 각 검토자 제공사(OpenAI, Google 등)로 전달됩니다.
- PR 답글은 로컬 `gh`에 로그인한 계정으로 달립니다. 답글 앞에 `🤖 Claude:`를 붙여 구분합니다.

## 라이선스

MIT

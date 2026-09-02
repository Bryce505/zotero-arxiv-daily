# Third-party skill provenance (superpowers, ponytail)

> Vendored, unmodified snapshots of the [superpowers](https://github.com/obra/superpowers) and
> [ponytail](https://github.com/DietrichGebert/ponytail) Claude Code skill packs — see the
> per-package sections below for upstream commit, license, and exactly what was copied.

本目录记录 `.claude/skills/`、`.claude/hooks/`、`.claude/commands/` 下部分内容的来源,
这些内容是从对应上游仓库的 `skills/`(和 `hooks/`)子目录直接搬运而来,**没有**通过
Claude Code 的 plugin/marketplace 安装机制安装,而是作为项目级 skill/hook 直接提交进
本仓库,任何 clone 本仓库的 VM/会话都会自动识别到。

来源仓库:[Bryce505/RoutineRun](https://github.com/Bryce505/RoutineRun/tree/master/.claude)
(该仓库的 `.claude/` 下还搬运了 `neat-freak`、`ppt-master` 两个技能包,本仓库按需
只取了下面两个,其余未搬运)。

## superpowers

- 上游仓库:https://github.com/obra/superpowers
- 拉取时的 commit:`b36e0829c6d0140e93cfef2ca599b1b07d4a7797`(v6.3.0)
- 许可证:MIT(见 `superpowers/LICENSE`,版权归 Jesse Vincent)
- 搬运内容:
  - `skills/*`(14 个技能)→ 原样复制到 `.claude/skills/`
  - `hooks/session-start`(SessionStart 钩子,注入 `using-superpowers` 技能作为上下文)
    → 复制到 `.claude/hooks/superpowers/`,并在
    `.claude/hooks/superpowers/skills/using-superpowers/SKILL.md` 保留了一份该脚本
    通过相对路径 `__dirname/../skills/using-superpowers/SKILL.md` 读取所需的副本
    (脚本本身未做任何修改)
  - 在 `.claude/settings.json` 的 `hooks.SessionStart` 里挂载了等价配置,用
    `$CLAUDE_PROJECT_DIR` 替代原来插件专用的 `${CLAUDE_PLUGIN_ROOT}`

## ponytail

- 上游仓库:https://github.com/DietrichGebert/ponytail
- 拉取时的 commit:`2ed6c52c9d7e5e56942508591085fd45dea277d3`(v4.9.0)
- 许可证:MIT(见 `ponytail/LICENSE`,版权归 Dietrich Gebert)
- 搬运内容:
  - `skills/*`(6 个技能)→ 原样复制到 `.claude/skills/`
  - `commands/*.toml` → 改写为 Claude Code 的 markdown slash command 格式,放在
    `.claude/commands/`(`{{args}}` 占位符改为 Claude Code 约定的 `$ARGUMENTS`,
    文案未改动)
  - `hooks/ponytail-*.js`(SessionStart / SubagentStart / UserPromptSubmit 三个钩子
    及其依赖的运行时模块)→ 原样复制到 `.claude/hooks/ponytail/`,并同样保留了一份
    `.claude/hooks/ponytail/skills/ponytail/SKILL.md`(`ponytail-instructions.js`
    通过相对路径读取)
  - 在 `.claude/settings.json` 的 `hooks` 里挂载了等价的 SessionStart /
    SubagentStart / UserPromptSubmit 配置,路径改成 `$CLAUDE_PROJECT_DIR` 下的
    实际位置,脚本内容未做修改

## 维护提示

- 以上均为"一次性搬运"的快照,不会随上游更新自动同步。以后要升级,重新克隆
  上游仓库对应 commit,重复上述复制步骤即可。
- `.claude/hooks/*/skills/.../SKILL.md` 是运行时钩子读取用的副本,和
  `.claude/skills/` 下面 Claude Code 实际识别的技能内容是重复存放的两份拷贝
  (设计如此,上游脚本原本就假设 `hooks/` 和 `skills/` 是同级目录,为了不改动
  脚本逻辑,选择了直接复制而不是软链接)。

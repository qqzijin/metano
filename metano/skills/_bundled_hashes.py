"""Bundled SKILL.md SHA-256 trust whitelist (AUTO-GENERATED).

Maps skills_data-relative paths to the SHA-256 of the pristine bundled
SKILL.md. ``SkillLoader`` refuses to load any ``trust: bundled`` skill whose
hash is missing from this map or does not match the on-disk content — tampered
or unknown bundled skills are rejected at load time (audit P2-2, fail-closed).

Do not edit by hand. Regenerate with:

    python3 scripts/generate_skill_hashes.py

Then sync the runtime copy: ``bash sync_runtime.sh``.
"""

BUNDLED_SKILL_HASHES = {
    'analysis/analyze/SKILL.md': '4d0d93f7a1dae4780e6e0851b4bfbff392a856ef19051e0746137ea8245f02d9',
    'analysis/brainstorm/SKILL.md': '7bee7306fde80406f783d93295484d561967262448d01eb80fd097e78fad355f',
    'autonomous-agents/claude-code/SKILL.md': 'b28b50c1bcc3e11b6e5650e4a5f2bcb2adb8dec50037abad587c6260908fb227',
    'creative/architecture-diagram/SKILL.md': '129c0370e6e9d9833f6f7c9bb3cce48c8e1a7bd770d7615fad2a73149301e839',
    'creative/ascii-art/SKILL.md': '1f548681be3e5b8539af1552c345ed95bdfa965aa08bd1110b36552a11072cf1',
    'creative/claude-design/SKILL.md': 'c56202b3a6439570260d88cb9df27fb74a41ff751c4fd456c489c1bcf2e52e9a',
    'creative/design-md/SKILL.md': '1bd2d5e0edad7b8d52996b47db99ebc18131d725f2e1803fd7e7e9cfadaaed74',
    'creative/excalidraw/SKILL.md': 'ce86e2f9134c44abe73dfdb19ed26605ed8ce345223c5f7e0f8948dd8fdc45bc',
    'creative/humanizer/SKILL.md': '891d32e1ce9e68cea06019b291858cb87f3ad07a157354fa9c8bb9d0cad0b9b0',
    'creative/popular-web-designs/SKILL.md': '538777c375eae03f8adbd4b6906fc15a679847b63fe67367719d0af6761c0f5c',
    'creative/sketch/SKILL.md': '650b739471c62576b2c1fffd4e9192c1cffae46128cd92314c15a4de4690fef5',
    'data-science/jupyter-live-kernel/SKILL.md': '4f95f1b77e4867884589a0e58957795ed14d31eba3853ff806f9f37d73b15794',
    'development/code-review/SKILL.md': '9a7e06983fed5bb21032321e3dabe630b0320eb2db12dad6130318be63f8c8e7',
    'development/debug/SKILL.md': '8da475fdd8cd97497137ce97c165d5c7d2d0fd444848b412fa8dad0dd4410a11',
    'development/hermes-agent-skill-authoring/SKILL.md': '402229abbc21d961ac8d16a85759b015c13ba163d8e453d274b794ca862600b4',
    'development/plan/SKILL.md': '3d06f39bfc8098c9e9f7ca77b2c56b40fac5d59edbac0e40075219fcee0dc27f',
    'development/requesting-code-review/SKILL.md': '427923c8dc382b1749d339810345a73ba6e67bb9eea2f29b883d7c9f019f2f34',
    'development/spike/SKILL.md': '577d80336020a307984c404b90f094d702f2eba26acfb669de528e1980589ec1',
    'development/subagent-driven-development/SKILL.md': 'a6a59b141b801003be955507bba23a9db1a2343047feed7e4e1ec547c2377717',
    'development/systematic-debugging/SKILL.md': '801f7f340bf6b64e267695fe9eca11676e8d5162770253568e9df859ea74cf89',
    'development/test-driven-development/SKILL.md': '8a0db647b7966e621ac4fce715ec32052189666d42a8fa7e0a53cad0c183efca',
    'development/writing-plans/SKILL.md': 'd3c9978a72f4a84ef555d70e9d0c22dae297aed5482d4478068dcdbf9f3f962b',
    'email/himalaya/SKILL.md': 'fa9cf05c93ba8379830626f5fd9c0c3576a6ef7750442814712a53cbf240dc2b',
    'github/codebase-inspection/SKILL.md': 'ed806738d6dc4252df8683cfe0429fde3bf91ed796143360336d486e2ae6cefb',
    'github/github-auth/SKILL.md': '10f228a5903c8376f4cd037176aa294da23bee496899a11dedc53651b5d071a1',
    'github/github-code-review/SKILL.md': '8ae58ee1fb7848812ce39130ee5a790469b609fbd3df54912a4c969de242692f',
    'github/github-issues/SKILL.md': '55c7ce4280ca9b41ed57898e93f1f88a12934d2a2aa51bc0f7a1a1b2f6068187',
    'github/github-pr-workflow/SKILL.md': 'a28cdf56dc0defc1b77589571ec0c3fedde1606c83990ae196fab2f63a02e11b',
    'github/github-repo-management/SKILL.md': '144c73d64d18b5323c93aaf19eff5af515a69ac6f48f08647dd3b60983ea1949',
    'mcp/native-mcp/SKILL.md': '2ef1644123c7261a3a74db3877004fe497fac5855750ff28ad541c5e9773dcef',
    'media/youtube-content/SKILL.md': '4e77681b81c7aa07ed9d511d745dd937b3a012c75d83629116fa203609c83164',
    'meta/explain/SKILL.md': '2b9bd8002c52b030e58b93a346417d421e53d2943aca3eea205a5ab2645d677e',
    'meta/skills-info/SKILL.md': 'ac5eaf7022ac024dc7ab313ea7847e9c55c5a16eb9326bcb64788e5da8088360',
    'note-taking/obsidian/SKILL.md': '3ca880e55a6959f9ca4078072532371e1bee0f9e432fb034a73a4f70d6af3f8a',
    'productivity/airtable/SKILL.md': '3c7efc90732c41888d2867a781698dd655e85767b30b9b47aab0944a2a0ebb50',
    'productivity/google-workspace/SKILL.md': 'f52e84ff81a76730e30415c7fe4453244116b2d7aef99c9f63f498d36a896085',
    'productivity/linear/SKILL.md': 'b05c0639a0f2078a4a072eaca88b02e5d0722aa2f43c3a4b43cdca95b3f4c694',
    'productivity/nano-pdf/SKILL.md': 'a98e1de5a749ed42e1ec348fc109d589d2f1c448579d19e76111540a5080060d',
    'productivity/notion/SKILL.md': '3072ff629173cb1323d3c3f0aac219046232aec14574d0f6f7679d58ec1fae2b',
    'productivity/ocr-and-documents/SKILL.md': '742cbcaad00d7af9eeb5309549e7c9ed28aadfedf89d7684967c7a3463a9049f',
    'productivity/powerpoint/SKILL.md': '47ed58ca8e10bd8cacd6fd2af524811ae7623ec122e5326989d8b4b25f4d7852',
    'productivity/summarize/SKILL.md': '3f8a18a4ce4a2ffe368a75bd52fb287cb00a0e6889badcfe6b4dfbc0f590ffaf',
    'productivity/translate/SKILL.md': '26b62b634734d0a49d1eb027d8f80c6b2a9a059e1c018c8ca238354b2e48fa69',
    'research/arxiv/SKILL.md': 'd75ad424a749dfe3ef277e276726136dabfb9511da6739f7a322d7f79c00b89b',
    'research/blogwatcher/SKILL.md': 'a9dc2946c274238ef65fa0ed8eb732ac82f78ec5180bafbc4afb72cbd132c764',
    'smart-home/openhue/SKILL.md': '5c3cd3f4487a0c7cdd1dc1d2c7810eea49d1e502538078970f7be2dcffe7c559',
    'social-media/xurl/SKILL.md': 'b5cc587cb037f4ad75d9ef9fde0a8d0747dfab29c4902218e378cf23062eb9a8',
}

"""端到端链路测试：从零建书 → 写 3 章 → 提交追踪 → 确定性审查（覆盖伏笔埋设与回收）。

整条链路只落在 tmp_path 里，不依赖网络、Node 与上游 story-setup 技能包：
- ``init_novel_project`` 建目录树与 ``.novel.json``（上游缺失时按 skipped 降级，不跳过用例）；
- 设定 / 大纲 / 正文由用例直接写文件，模拟作者与写作 Agent 的真实产出；
- ``novel_track`` 转发 vendored ``tracking_commit.py``：init 一次 + append 三次；
- ``novel_status`` / ``novel_audit`` 做只读核对，要求追踪与正文对齐（S1 必须为 0）。

另有两条链路覆盖追加提交之外的分支：
- ``mode="revision"``：同章改稿重提，追踪里该章条目被改写而不是新增一条；
- ``novel_audit(scope="chapter", chapter=N)``：只审指定的第 N 章，finding 不串到别的章。
"""

from __future__ import annotations

import json

from pathlib import Path

from core import novel_service

# 伏笔物件：第 001 章埋设，第 003 章由正文明确回收
FORESHADOW_TEXT = "半枚铜钱"

# 第 001 章：埋设"半枚铜钱"，结尾留出下一章的钩子
CHAPTER_1 = """# 第001章 开端

北城门在黄昏时最难挤。沈舟把斗笠压低，跟着一队驮盐的骡子往里走，脚底踩着冻硬的泥，一步一滑。

他不敢走快。守门的军卒正逐个翻看路引，竹牌在手里磕出干脆的响声，翻完一个便抬手一挥，那人就小跑着进了城门洞。沈舟把怀里的路引摸出来，边角早已被汗浸软。名字是别人的，籍贯也是别人的，只有那张脸是自己的，他把脸藏在斗笠的阴影里，像藏一件不该带出门的东西。

轮到他的时候，军卒只抬眼扫了一下。你这引上的县，去年遭了水，人都跑光了，怎么还有你这样的后生？沈舟听见自己的心跳顶在喉咙上，嘴上却答得平：跑出来才有活路。军卒盯了他一会儿，笑了，把路引丢回他怀里，挥手让他过去。

进城之后他没有立刻走远。街边的灯笼还没点上，卖汤饼的摊子冒着白汽，一个瘦孩子蹲在墙根，把铜钱一枚一枚码整齐，好像在数自己的命。沈舟站了一会儿，才往车队常歇脚的车马店去。街面上有人在收摊，木板的碰响一层层传过来，他随着那声音走，尽量让自己的脚步和旁人一样慢。

路上他摸了摸怀里的东西。那是半枚铜钱，断口毛糙，边缘被磨得发亮，另一半早不知去了哪里。他不记得是谁给他的，只记得八岁那年冬天很冷，有人把这半枚铜钱按进他手心，说拿着，往后有人凭这个认你。那时他不懂，后来懂了也没用——这么多年，没人凭它来认过他。

他把半枚铜钱塞回贴身的布兜，绳子系了两道。车马店的门帘在他面前掀开，热气和牲口味一起涌出来。他迈步进去，听见有人在角落里喊了一声：还有一个位置，谁走？

店里人声不齐，火塘烧着湿柴，烟把人眼熏得发酸。沈舟挑了个靠门的位置坐下，要一碗热水，把干饼掰开泡软。邻桌几个脚夫正在争路线，一个说走东路稳，一个说东路查得严，走西路多绕两天却少事。沈舟听着，把每句话都记在心里：他需要一个不问来历的车队，也需要一个肯带他出山口的人。

最后他挑了一支驮盐的队伍。车头姓韩，人都喊他韩七，说话不多，只问了两句：能扛货吗，脚程怎么样。沈舟说能，说走过山路。韩七看了他一会儿，点了点头，说天亮前在门外等，晚了就自己走。沈舟应下，回身去付水钱，铜板在柜台上被他一枚一枚点清，点到最后，指尖停在贴身的布兜外，隔着一层布按了按。

沈舟举了举手。他要在天亮之前混进一支商队，出这座城，走到他该去的地方。
"""

# 第 002 章：推进剧情，让读者看到伏笔在别人身上起了反应
CHAPTER_2 = """# 第002章 推进

天没亮车队就动了。沈舟被分到最后一辆车上，和盐袋挤在一起，车厢窄，他的膝盖顶着前面车板的木棱，颠一下疼一下。

赶车的是个四十来岁的汉子，姓韩，单名一个七字，众人都喊他韩七。韩七不爱说话，鞭子甩得极准，落点永远在骡子耳后半寸的地方，从不碰皮肉。走到第一处岔口，他忽然回头看了沈舟一眼：你从北城进来的？沈舟说，是。韩七说，北城门今年查得紧，你能进来，命硬。

晌午在山坳里歇脚。众人围着火堆啃干饼，有人讲起山那边的事，说最近道上不太平，有人专在快出山口的地方收过路钱，不给就卸车轮。韩七听着，把鞭子横在膝上，指尖叩着鞭柄，一下，又一下。

沈舟摸出那半枚铜钱，假装擦拭，故意让它露在掌心。火光照着断口，铜色发暗。韩七的指尖停住了，停了约莫三息的工夫，他把视线挪开，去拨火，语气平得像在说天气：哪来的？沈舟说，路上捡的。韩七说，捡来的东西别乱拿出来，道上忌讳。

说这话的时候，他的喉结动了一下。

车队走到山脊上时，风变硬了，盐袋上的麻布被吹得鼓起又塌下去。沈舟把外衫掖紧，听韩七讲这条道上的规矩：过了三道梁就没有水，得在第二道梁的背阴处把水囊灌满；骡子走夜路会惊，惊了就停，别打，打只会更糟。沈舟一一应下，也把韩七说话时避人的小动作记下来——凡提到出山口，他都不看人。沈舟忽然想起八岁那年的冬天，那个人也是这么说话，不看人，声音压在喉咙里。他不知道自己为什么在这时候想起这些，也许是风太冷，冷得人心里只剩下最旧的那件事。

下午的车队没出事。到了太阳快落山，前面果然有人拦路。三个人，一根横木往路当中一搭，开口就要价。众人脸色都变了，韩七却没慌，跳下车，走过去谈了半柱香，回来时脸沉着，说给一半，先过去。车队把两袋盐推下车，横木抬起。

过了卡子，沈舟坐在车尾回头看。那三个人正围着盐袋分东西，其中一个瘦高个儿腰间挂着一串旧铜钱，走一步响一声。沈舟的手在袖子里收紧了。

天黑透才到山谷里的驿站。院墙塌了一角，用荆条补着。韩七把骡子拴好，转身对沈舟说，你跟我来。沈舟跟着他走到院后的柴堆边。韩七站住，没回头，只问：那半枚铜钱，你留着多久了？沈舟说，十几年。韩七沉默了很久，说到站再说，今晚先睡。

沈舟躺下时，听见隔壁有人在磨刀。他睁着眼睛，直到驿站最后一盏灯灭掉。
"""

# 第 003 章：正文明确回收"半枚铜钱"（两半合上，断口吻合）
CHAPTER_3 = """# 第003章 回收

天亮时驿站起了雾。沈舟没睡好，起身时肩胛骨像被谁用石头压了一夜。

他在井边打水洗脸，韩七已经在那儿了，蹲在一块青石上，手里转着件东西。听见脚步声，韩七把它扣在掌心，抬头看他，说，过来。

沈舟走过去。韩七把掌心翻开，那里躺着半枚铜钱的另外一半，断口也毛糙，边缘同样磨得发亮。

沈舟把怀里那半枚掏出来。韩七伸手，两个半片在他掌心里合上，断口高低互咬，缺口严丝合缝，一枚完整的铜钱就这么拼了起来。铜面上的字只有一半是自己的，另一半是对方的，合起来才看得出本来是个什么字。

沈舟的手有点抖。他说，我八岁那年，有人把这半枚铜钱按在我手里。韩七说，我知道，那人力气很大，把你的手都捏红了。沈舟抬头，韩七笑了笑，那笑很浅，说，那个人是我哥。

雾在院墙上淌。韩七把拼好的铜钱捏了捏，又分开，把半片递回给沈舟：留着。两半合起来才算数，缺一半，谁都不认。沈舟说，你哥呢？韩七把另外半片收进贴身的口袋，站起来，说，死了，死在出山口的旧道上，离昨天那个卡子不到三里。

沈舟把半枚铜钱重新塞回布兜，绳子系了两道。这一次它的分量不一样了，它不再是件没用的旧物，而是一个人临死前递出去的东西。

他们回到驿站院里时，众人正往车上装盐。韩七把半片铜钱收进内袋，抬手在沈舟肩上按了一下，说了句不该带到道上的话：我哥当年也是替人押盐，押到第三趟，人没了，盐还在。这行当里货比人值钱，你记着。沈舟没应声，只把布兜的绳子又紧了一道。

院里有人喊开拔。沈舟去牵自己的骡子，那骡子昨夜受了风，鼻息发烫，他用手背贴了一会儿，解下水囊喂了两口。韩七站在车头等他，见他过来，把鞭子递过去，说你试试。沈舟接住，手腕一沉——鞭子比他想的沉，鞭柄被手汗磨得发亮，木纹里嵌着黑色的旧污。

车队出发的时候，沈舟坐到了前面第二辆车，也就是韩七身边。鞭子甩出去，落在骡子耳后半寸的地方，不碰皮肉。韩七说，你要去的那个地方我知道怎么走，走之前我们得先回一趟旧道。沈舟说，好。

骡子喷出两股白汽，车轮碾过霜。沈舟摸了摸布兜，那半枚铜钱还在，边角硌着手心，像一个终于被认下的约定。
"""


def _write(root: Path, rel: str, text: str) -> Path:
    """按项目相对路径写文本文件（父目录按需创建）。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _position(chapter: int) -> dict:
    """tracking_commit.py 要求的 context.position（四个字段缺一不可）。"""
    return {
        "volume": "第001卷",
        "volume_start_chapter": 1,
        "story_time": f"第{chapter}日 黄昏",
        "scene": "北城门" if chapter == 1 else "山谷驿站",
    }


def _init_document() -> dict:
    """novel_track(init) 的最小合法输入：0 章基线，无角色、无伏笔。"""
    return {
        "schema_version": 1,
        "book_title": "长夜行",
        "last_chapter": 0,
        "context": {
            "position": _position(1),
            "long_term_constraints": [],
            "active_character_names": [],
            "continuity_risks": [],
            "recent_chapters": [],
            "next_chapter_commitments": ["天亮随车队出城"],
        },
        "character_snapshots": {},
        "foreshadow": [],
        "timeline_events": [],
    }


def _append_document(
    chapter: int,
    expected_revision: int,
    result: str,
    foreshadow_changes: list[dict] | None = None,
) -> dict:
    """novel_track(append) 的最小合法事务：一次追加一章，修订号逐章递增。"""
    return {
        "schema_version": 1,
        "mode": "append",
        "chapter": chapter,
        "chapter_title": ("开端", "推进", "回收")[chapter - 1],
        "expected_state_revision": expected_revision,
        "delta": {
            "result": result,
            "character_changes": [],
            "foreshadow_changes": foreshadow_changes or [],
            "timeline_events": [],
            "constraints": [],
            "next_chapter_commitments": ["继续赶路"],
            "retired_context_items": [],
            "retired_characters": [],
        },
        "context": {
            "position": _position(chapter),
            "long_term_constraints": [],
            "active_character_names": [],
            "continuity_risks": [],
        },
        "character_snapshots": {},
    }


def _foreshadow(status: str, planted: int = 1, planned: int | None = 3) -> dict:
    """伏笔事务条目：第 001 章埋设，第 003 章回收（planned 不得早于 planted）。"""
    return {
        "action": "upsert",
        "id": "F001",
        "summary": f"{FORESHADOW_TEXT}：断口可以拼合的另一半",
        "planted_chapter": planted,
        "planned_resolution_chapter": planned,
        "status": status,
        "importance": "高",
    }


def _revision_document(
    chapter: int,
    expected_revision: int,
    result: str,
    chapter_title: str | None = None,
    foreshadow_changes: list[dict] | None = None,
) -> dict:
    """novel_track(revision) 的事务：与 append 同构，只是 ``mode`` 不同。

    脚本对 revision 的硬约束（读 tracking_commit.py 确认）：``chapter`` 必须是已提交章
    （``chapter <= last_committed_chapter``），``expected_state_revision`` 必须等于当前
    修订号，且上下文条目要整份重提、不得在修订里退役条目。本用例的上下文始终为空列表，
    因此直接沿用 append 的事务骨架。
    """
    document = _append_document(chapter, expected_revision, result, foreshadow_changes)
    document["mode"] = "revision"
    document["chapter_title"] = chapter_title or f"{('开端', '推进', '回收')[chapter - 1]}（改稿）"
    return document


def _new_book(root: Path) -> Path:
    """建一个最小书项目（建书 + 设定 + 卷纲）；正文与细纲由各用例按需补。"""
    init = novel_service.init_novel_project(
        str(root), "长夜行", genre="武侠", platform="起点", target_words=300000,
    )
    assert init["ok"] is True, init
    assert (root / novel_service.NOVEL_MARKER).is_file()
    _write(root, "设定/题材定位.md", "# 题材定位\n\n- 类型：武侠\n- 主线：断掉的凭据要拼回来\n")
    _write(root, "设定/文风.md", "# 文风\n\n- 冷峻克制，短句为主\n")
    _write(
        root,
        "大纲/卷纲_第001卷.md",
        "\n".join(
            [
                "# 第001卷 卷纲",
                "",
                "> 作用域：卷级常任",
                "## 全卷基调",
                "- 冷峻、克制，不写爽感外溢",
                "",
                "> 作用域：单元级 D1-01",
                "## 剧情单元 D1-01 出城",
                "- 单元ID：D1-01",
                "- 章节范围：第1-3章",
                "- 目标：沈舟混入商队出城，并在驿站认下那半枚铜钱的来历",
                "",
            ]
        ),
    )
    return root


def _write_detail_outline(root: Path, chapter: int, target: int) -> Path:
    """按 大纲/细纲_第NNN章.md 契约写细纲；字数目标决定审计的字数下限。"""
    return _write(
        root,
        f"大纲/细纲_第{chapter:03d}章.md",
        "\n".join(
            [
                f"# 第{chapter:03d}章 细纲",
                "",
                f"字数目标: {target}",
                f"- 本章目标：推进第 {chapter} 个情节点",
                "",
            ]
        ),
    )


def _write_body(root: Path, chapter: int, name: str, text: str) -> Path:
    """按 正文/第NNN章_章名.md 契约写一章正文。"""
    return _write(root, f"正文/第{chapter:03d}章_{name}.md", text)


def test_novel_flow_init_write_commit_audit(tmp_path):
    """建书 → 写设定/大纲/正文 → 三次追踪提交 → 状态与审计全链路验证。"""
    root = tmp_path / "长夜行"

    # 1. 从零建书：目录树 + .novel.json（上游技能包不参与本用例的断言）
    init = novel_service.init_novel_project(
        str(root), "长夜行", genre="武侠", platform="起点", target_words=300000,
    )
    assert init["ok"] is True, init
    assert (root / novel_service.NOVEL_MARKER).is_file()
    for rel in ("设定", "大纲", "正文", "追踪", "追踪/逐章记录"):
        assert (root / rel).is_dir(), f"建书缺少目录 {rel}"

    # 2. 最小设定：题材定位与文风
    _write(root, "设定/题材定位.md", "# 题材定位\n\n- 类型：武侠\n- 主线：断掉的凭据要拼回来\n")
    _write(root, "设定/文风.md", "# 文风\n\n- 冷峻克制，短句为主\n- 情绪落在动作与物件上\n")

    # 3. 卷纲 + 三章细纲（每章字数目标 800）
    _write(
        root,
        "大纲/卷纲_第001卷.md",
        "\n".join(
            [
                "# 第001卷 卷纲",
                "",
                "> 作用域：卷级常任",
                "## 全卷基调",
                "- 冷峻、克制，不写爽感外溢",
                "",
                "> 作用域：单元级 D1-01",
                "## 剧情单元 D1-01 出城",
                "- 单元ID：D1-01",
                "- 章节范围：第1-3章",
                "- 目标：沈舟混入商队出城，并在驿站认下那半枚铜钱的来历",
                "",
            ]
        ),
    )
    for chapter, title in ((1, "开端"), (2, "推进"), (3, "回收")):
        _write(
            root,
            f"大纲/细纲_第{chapter:03d}章.md",
            "\n".join(
                [
                    f"# 第{chapter:03d}章 细纲 {title}",
                    "",
                    "字数目标: 800",
                    f"- 本章目标：推进第 {chapter} 个情节点",
                    "",
                ]
            ),
        )

    # 卷纲取段链路：单元锚点能取到对应段（含卷级常任）
    outline = novel_service.novel_outline(str(root), unit_id="D1-01", stage="write")
    assert outline["ok"] is True, outline
    assert "剧情单元 D1-01 出城" in outline["text"]

    # 4. 三章正文（每章 800 字以上；第 001 章埋伏笔，第 003 章正文回收）
    bodies = {1: CHAPTER_1, 2: CHAPTER_2, 3: CHAPTER_3}
    for chapter, name in ((1, "开端"), (2, "推进"), (3, "回收")):
        _write(root, f"正文/第{chapter:03d}章_{name}.md", bodies[chapter])
        counted = novel_service.novel_wordcount(
            workdir=str(root), chapter=chapter,
        )
        assert counted["ok"] is True, counted
        assert counted["visible_chars"] >= 800, f"第 {chapter} 章正文不足 800 字"

    # 第 001 章埋、第 002 章提及（别人有反应）、第 003 章正文明确回收
    assert FORESHADOW_TEXT in bodies[1]
    assert FORESHADOW_TEXT in bodies[3]
    assert "严丝合缝" in bodies[3]

    # 5. 提交追踪：先 init，再三次 append（第 001 章埋、第 003 章回收同一伏笔）
    track = novel_service.novel_track(str(root), "init", _init_document())
    assert track["ok"] is True, track
    assert track["last_committed_chapter"] == 0

    commits = (
        (1, "主角在黄昏混入驮盐车队", [_foreshadow("已埋")]),
        (2, "车队过收路钱的卡子，半枚铜钱让韩七变了脸色", None),
        (3, "两半铜钱拼合，身世凭据被认下", [_foreshadow("已回收")]),
    )
    for chapter, result, foreshadows in commits:
        payload = novel_service.novel_track(
            str(root),
            "append",
            _append_document(chapter, chapter - 1, result, foreshadows),
        )
        assert payload["ok"] is True, payload
        assert payload["last_committed_chapter"] == chapter
        assert payload["state_revision"] == chapter
        assert (root / "追踪" / "逐章记录" / f"第{chapter:03d}章.md").is_file()

    # 事务临时文件不留在项目里
    assert not list(root.glob("novel-track-*.json"))
    state = json.loads(
        (root / "追踪" / novel_service.TRACKING_STATE_NAME).read_text(encoding="utf-8")
    )
    assert state["last_committed_chapter"] == 3
    assert state["foreshadow"]["F001"]["status"] == "已回收"

    # 6. 状态：三章已写、下一章为 4、有累计字数、没有未回收的活跃伏笔
    status = novel_service.novel_status(str(root))
    assert status["is_novel"] is True
    assert status["chapters_written"] == 3
    assert status["next_chapter"] == 4
    assert status["total_words"] > 0
    assert status["last_commit"] == 3
    assert status["active_foreshadows"] == 0

    # 7. 审查：追踪与正文对齐（S1=0），且没有字数不达标 / 细纲缺口
    audit = novel_service.novel_audit(str(root), scope="all")
    assert audit["ok"] is True, audit
    assert audit["scope"] == "all"
    assert audit["summary"]["S1"] == 0, audit["findings"]
    assert audit["summary"]["S2"] == 0, audit["findings"]  # 伏笔已在第 003 章回收
    assert audit["summary"]["S3"] == 0, audit["findings"]  # 字数达标且无细纲缺口
    assert [f for f in audit["findings"] if f["type"] == "tracking_state"] == []
    # AI 味检查已是本地确定性扫描：正文干净时不出 finding，也不再进 skipped
    assert [f for f in audit["findings"] if f["type"] == "ai_patterns"] == []
    assert not [s for s in audit["skipped"] if s["check"] == "ai_patterns"]

    # 8. 回收可被正文验证：第 003 章明确写出伏笔物件
    chapter_3_text = (root / "正文" / "第003章_回收.md").read_text(encoding="utf-8")
    assert FORESHADOW_TEXT in chapter_3_text


def test_novel_revision_updates_committed_chapter_entry(tmp_path):
    """append 第 1 章 → 同章 revision 改稿：条目被改写而非新增，last_committed_chapter 不动。"""
    root = _new_book(tmp_path / "长夜行")
    _write_detail_outline(root, 1, 800)
    _write_body(root, 1, "开端", CHAPTER_1)

    init = novel_service.novel_track(str(root), "init", _init_document())
    assert init["ok"] is True, init

    draft_result = "初稿：主角在黄昏混入驮盐车队"
    draft = novel_service.novel_track(
        str(root), "append", _append_document(1, 0, draft_result, [_foreshadow("已埋")]),
    )
    assert draft["ok"] is True, draft
    assert draft["last_committed_chapter"] == 1
    assert draft["state_revision"] == 1

    delta_path = root / "追踪" / "逐章记录" / "第001章.md"
    delta_before = delta_path.read_text(encoding="utf-8")
    assert draft_result in delta_before
    assert "开端" in delta_before

    # 改稿：结果文案与伏笔回收计划都改了（正文文件本身另由作者改写，这里只提交追踪）
    revised_result = "改稿：主角改从西门混入车队，删掉与主线无关的路人"
    revised_foreshadow = {
        **_foreshadow("已埋"),
        "planned_resolution_chapter": 4,
        "summary": "改稿后：半枚铜钱改在驿站埋设",
    }
    revision = novel_service.novel_track(
        str(root),
        "revision",
        _revision_document(1, 1, revised_result, foreshadow_changes=[revised_foreshadow]),
    )
    assert revision["ok"] is True, revision
    # 修订不推进章号，只前进修订号
    assert revision["last_committed_chapter"] == 1
    assert revision["state_revision"] == 2

    # 逐章记录里仍然只有第 001 章一条：是改写，不是新增
    assert sorted(p.name for p in (root / "追踪" / "逐章记录").glob("第*章.md")) == ["第001章.md"]
    delta_after = delta_path.read_text(encoding="utf-8")
    assert revised_result in delta_after
    assert draft_result not in delta_after
    assert "（改稿）" in delta_after

    state = json.loads(
        (root / "追踪" / novel_service.TRACKING_STATE_NAME).read_text(encoding="utf-8")
    )
    assert state["last_committed_chapter"] == 1
    assert state["state_revision"] == 2
    # 该章在上下文里的条目被就地更新（仍是一条）
    assert state["context"]["recent_chapters"] == [{"chapter": 1, "summary": revised_result}]
    # 伏笔条目同样被更新，没有长出第二条
    assert list(state["foreshadow"]) == ["F001"]
    assert state["foreshadow"]["F001"]["summary"] == revised_foreshadow["summary"]
    assert state["foreshadow"]["F001"]["planned_resolution_chapter"] == 4
    assert state["foreshadow"]["F001"]["updated_chapter"] == 1

    # 派生视图同步到新修订号；事务临时文件不留在项目里
    assert "状态修订：2" in (root / "追踪" / "上下文.md").read_text(encoding="utf-8")
    assert not list(root.glob("novel-track-*.json"))

    # 修订后追踪仍与正文对齐：审计不报 S1
    audit = novel_service.novel_audit(str(root), scope="all")
    assert audit["ok"] is True, audit
    assert audit["summary"]["S1"] == 0, audit["findings"]

    # revision 不能越界写入未提交章：第 2 章还没写，脚本按章号守卫拒绝
    out_of_range = novel_service.novel_track(
        str(root), "revision", _revision_document(2, 2, "越界修订"),
    )
    assert out_of_range["ok"] is False
    assert "cannot revise unwritten chapter" in (out_of_range["error"] + out_of_range["stderr"])
    # 被拒事务不改状态
    assert json.loads(
        (root / "追踪" / novel_service.TRACKING_STATE_NAME).read_text(encoding="utf-8")
    )["state_revision"] == 2


def test_novel_audit_chapter_scope_only_reports_specified_chapter(tmp_path):
    """两章项目里只有第 2 章字数不足：chapter=2 的审计只出第 2 章的 finding。"""
    root = _new_book(tmp_path / "长夜行")

    # 第 001 章：细纲目标 400，正文 800 字以上 → 达标，不产生 finding
    _write_detail_outline(root, 1, 400)
    _write_body(root, 1, "开端", CHAPTER_1)
    # 第 002 章：细纲目标 2000，正文只有一句话 → 可见字数远低于 80%，是唯一的 finding 来源
    _write_detail_outline(root, 2, 2000)
    _write_body(root, 2, "推进", "# 第002章 推进\n\n天没亮车队就动了。\n")

    init = novel_service.novel_track(str(root), "init", _init_document())
    assert init["ok"] is True, init
    for chapter in (1, 2):
        track = novel_service.novel_track(
            str(root), "append", _append_document(chapter, chapter - 1, f"第 {chapter} 章提交"),
        )
        assert track["ok"] is True, track
        assert track["last_committed_chapter"] == chapter

    # 显式指定第 2 章：finding 只应指向该章
    audit = novel_service.novel_audit(str(root), scope="chapter", chapter=2)
    assert audit["ok"] is True, audit
    assert audit["scope"] == "chapter"
    assert len(audit["findings"]) == 1, audit["findings"]
    finding = audit["findings"][0]
    assert finding["type"] == "chapter_wordcount"
    assert finding["level"] == "S3"
    assert finding["file"] == "正文/第002章_推进.md"
    assert "第2章" in finding["message"]
    # 全部 findings 都落在指定章，不含其它章的条目
    assert {f["file"] for f in audit["findings"]} == {"正文/第002章_推进.md"}
    assert all("第001章" not in f["file"] for f in audit["findings"])
    assert all("第1章" not in f["message"] for f in audit["findings"])
    assert audit["summary"] == {"S1": 0, "S2": 0, "S3": 1, "S4": 0}
    assert not [s for s in audit["skipped"] if s["check"] == "ai_patterns"]

    # 对照：同一项目指定第 1 章 → 没有任何 finding（若 chapter 参数被忽略而退化成"最新章"，
    # 这里会报出第 2 章的问题，因此这条断言证明确实按章号收窄了范围）
    clean = novel_service.novel_audit(str(root), scope="chapter", chapter=1)
    assert clean["ok"] is True, clean
    assert clean["findings"] == []
    assert clean["summary"] == {"S1": 0, "S2": 0, "S3": 0, "S4": 0}

    # 对照：scope=all 扫全项目，仍只有第 2 章那一条（第 1 章始终达标，无 S1 噪声）
    every = novel_service.novel_audit(str(root), scope="all")
    assert every["ok"] is True, every
    assert every["summary"] == {"S1": 0, "S2": 0, "S3": 1, "S4": 0}
    assert [f["file"] for f in every["findings"]] == ["正文/第002章_推进.md"]

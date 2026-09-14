# 近视／眼科文献的独立云端更新程序

这个工程只处理公开题录和机器核验状态。Python 标准库运行，不需要 Codex、ChatGPT、生成式模型、收费 API 或用户电脑常开。原有本地 HTML、详细笔记、研究方案、全文、图片、附件与阅读数据不在这个工程中。

## 运行

```sh
python3 -m unittest discover -s tests -v
python3 updater.py run --state-dir state --output public
python3 updater.py republish --state-dir state --output public
```

`domain.json` 集中定义领域、六组查询、纳入规则、30日重叠、首次90日回溯、28日触发365日补查、接口批量和核验预算。每条查询分别运行 `crdt` 与 `lr`，分页完整后才推进该查询的水位。PubMed 超过9,999条时按日期递归拆分；单日仍超限、记录丢失或请求失败会明确报错，保留此前水位，下一次重试。期刊论文与书籍章节分别解析，不用章节编辑者冒充作者。

日期依据 [PubMed 官方说明](https://pubmed.ncbi.nlm.nih.gov/help/)：XML历史中的 `entrez` 对应CRDT，`pubmed` 对应EDAT；题录的 `DateRevised` 对应LR，出版社的 `revised` 不代替LR。年月精度原样保留。接口上限依据 [E-utilities 文档](https://www.ncbi.nlm.nih.gov/books/NBK25499/)。书籍结构依据 [PubMed DTD](https://dtd.nlm.nih.gov/ncbi/pubmed/doc/out/190101/el-PubmedBookArticle.html)。

## 身份、全文与阅读范围

PMID和规范化DOI用于稳定去重；相似题名不能强制合并，标识冲突独立保存在 `conflicts`。预印本和正式版保留各自条目，仅当Crossref返回明确关系时关联。更正、撤稿、关注声明醒目标记。

主线相关性和跨领域迁移理由由固定规则判断；规则不能明确判断的条目进入待核对。跨领域迁移点是候选设想，不能当作近视机制或疗效证据。自动记录只提供题录和来源，未精读、未形成中文详细笔记，研究类型不确定时不猜测为动物实验或临床试验。代码、数据、权重、许可证未专项核验时标注未能确认。

全文A要求实际取得结构化主文、匹配PMID或DOI且题名匹配；B只有正式索引；C未确认全文。A不是精读或质量背书。403、超时、验证码、错误文章或登录页面不能变成A。Crossref和正文核验均为有预算的轮转队列，核验时间不会用本轮检索时间冒充。Europe PMC的MED题录是PubMed镜像，不计作独立证据。摘要仅在内存中辅助分类，原始摘要/全文不保存、不发布。

## GitHub Actions 与持久化

`.github/workflows/weekly.yml` 在默认分支生效：每周一01:17 UTC（北京时间09:17）运行，支持手动运行及仅重发已有数据。`concurrency` 串行执行，不取消正在写状态的作业。GitHub调度可能延迟，公开仓库长期无活动可能停用调度，参见 [Actions 官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。

每个全新runner从 `data` 分支恢复 `state/` 和历史 `public/`。检索后先将状态提交到该分支，再上传Pages artifact并部署。artifact仅是传输载体，`data`分支才是长期存储。远程Git写入失败会停止发布，避免出现不可恢复的已发布数据。没有临时cache作为唯一数据库。

`public/snapshots/<SHA256>.json` 是累计不可变快照；`manifest.json` 最后写入，记录领域、基线版本、数据版本、完整性哈希、查询进度、失败原因和计数。新快照校验失败时不切换旧清单。发布入口只包含JSON和 `.nojekyll`，不会托管本地阅读HTML。

部署后 `verify_deployment.py` 从真实HTTPS入口重新取回清单和快照，检查内容哈希与 `Origin: null` 请求下的 `Access-Control-Allow-Origin: *`。只有这个检查成功才写入 `data/publication-status.json` 的成功回执。回执通过GitHub raw HTTPS读取；它是独立的部署后状态，避免部署前把“计划发布时间”写成“已成功发布”。清单中的最近成功发布时间是生成清单时已知的成功记录，最新回执优先。发布失败也提交结果，保留上次成功时间；手动 `republish` 可从已持久化的数据重试。

所需权限仅为此仓库的 `contents: write`、`pages: write` 和 `id-token: write`；使用GitHub内置令牌，不添加模型或第三方私密令牌。Pages使用 [官方Actions发布方式](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages/)。代码中的动作版本固定到主版本，未固定不可变提交SHA；维护时应检查上游兼容性。

## 本地阅读器接通与验收

本地生成源 `work/cloud_updates.mjs` 和 `reader_config.json` 留在用户电脑。得到公开许可并核实正式仓库/Pages后，配置唯一的清单URL、回执URL和 `approved_publication: true`，重建同一个本地HTML。打开时先显示279篇基线及原笔记，再以无凭证CORS请求读取累计版本，核验领域、基线、哈希、结构与链接协议。不会跳转到在线网站，也不会写回磁盘或导出。

验收必须分别完成：两次真实云端运行且使用不同runner；第二次恢复 `data` 分支而非临时文件；正式HTTPS内容与CORS核验；真实Chrome `file://` 本地阅读（包括搜索、分类、累计补抓、失败保留原内容）。仅DOM模拟通过不能代替这些验收。工作流启用和观察到真实 `schedule` 事件要分别记录。

`tests/` 含隔离的人工测试数据，用于日期、分页、恢复、去重、隐私和错误处理；测试数据不会进入发布目录。当前覆盖是PubMed加有限Crossref关系核验，不包括所有中文订阅库或所有非PubMed预印本，不能保证绝无遗漏。

## 收录年限（2026-09-14用户调整）

普通新题录仅保留滚动近三年发表；明确期刊白名单内的文献放宽至五年。白名单和正规刊名别名集中在domain.json，不按出版社名称前缀或模糊匹配扩展。该名单是本库的收录选择，不是声称普适的影响因子排名；包含IOVS等眼科专业核心刊。

日期依据最早可确认的电子／期刊发表日期，不能用最近修订、数据库新收录时间替代。部分日期跨越边界时暂不纳入；不补造具体日期。每轮检索先限五年，取得题录后严格执行三年／期刊五年条件；每次发布也重新筛选整个累计状态，防止存量老题录重新进入当前快照。历史状态与旧快照保留用于审计。

既有基础ID的机器题录仍可核查更正信息。当前以及未来完成的详细笔记均在本地长期保留，不受年份／期刊限制；本地生成器持久记录完成过笔记的ID。此完成状态和笔记内容不上传公开仓库。

### JCR Q1 admission (2026-09-14)

Unfinished/new records must pass both the rolling publication window (3 years, or 5 years for the exact curated journal list) and verified JCR **JIF Quartile Q1**. Unknown quartiles are retained in an independent review queue, not formally admitted; known Q2–Q4 records are omitted from the active release. Historical state is retained. Base metadata exceptions permit correction checks without publishing private note-completion status.

`domain.json` records the 2026 JCR release (2025 data), public source URL and SHA-256, verification date, exact journal titles, eISSNs, source-page locators, source-reported quartiles and PubMed/ISSN-verified aliases for journals encountered in this library. The source is a table publicly reproduced by the Nikolaev Institute of Inorganic Chemistry; it is not a direct authenticated JCR database query. It does not provide category-specific quartiles; none are inferred. Unknown titles, unavailable JIF values, and future unverified editions never auto-pass. The ranking edition is pinned until another edition is verified; weekly literature discovery does not update annual JCR data automatically. No impact factor cutoff, SJR or AIS quartile is used.

The local HTML keeps an append-only private ledger of every completed detailed note/method analysis, including future completions. These are permanently exempt from age and journal filters. The ledger, HTML, original notes and full texts are not uploaded.

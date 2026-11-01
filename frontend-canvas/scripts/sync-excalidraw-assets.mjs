/**
 * 把 @excalidraw/excalidraw 包内的 fonts/ 与 locales/ 同步到 public/。
 *
 * 原因：Excalidraw 0.18 的字体是通过 JS 的 FontFace API 动态 fetch 的。
 * 当 window.EXCALIDRAW_ASSET_PATH 未设置时，createUrls() 只会生成
 *   https://esm.sh/@excalidraw/excalidraw@0.18.1/dist/prod/<uri>
 * 这一条 CDN 地址，本地不参与尝试 —— 离线环境会全部失败。
 * 因此这里把官方字体与语言包复制到 public/（Vite 会原样拷进 dist/），
 * 并在 index.html 里把 EXCALIDRAW_ASSET_PATH 指向站点自身的相对根目录。
 *
 * 例外：中文手写体 Xiaolai 不复制（见下方 EXCLUDED_FONT_FAMILIES 注释）。
 *
 * 目标目录 public/fonts、public/locales 属于构建中间产物，见 .gitignore。
 */
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const projectRoot = path.resolve(here, '..')
const prodDir = path.join(
  projectRoot,
  'node_modules',
  '@excalidraw',
  'excalidraw',
  'dist',
  'prod',
)

/**
 * 不参与同步的字体族。
 *
 * 为什么砍 Xiaolai：它是 Excalidraw 的中文手写体，按 GB2312 分片成 209 个 woff2，
 * 单目录约 12MB，而其余 8 个字体族加起来只有约 0.4MB —— 它一个就占了 fonts/ 的 96%。
 * 构建产物体积主要就压在这一坨上，收益/代价比最低，所以直接不同步。
 *
 * 跳过的后果（可接受）：
 * - 画布功能、其它字体族（Virgil/Excalifont/Cascadia/ComicShanns/Nunito/Lilita/Liberation/Assistant）
 *   与语言包 locales 全部不受影响；
 * - 只有把文字设成「中文手写体」时，中文会回退到系统后备字体显示（不再是手写观感）；
 * - Excalidraw 是用 JS 的 FontFace API 异步加载字体的，加载失败不会阻塞画布渲染，
 *   只是控制台会有该字体加载失败的提示，属于预期行为。
 */
const EXCLUDED_FONT_FAMILIES = new Set(['Xiaolai'])

/** cpSync 的 filter：命中排除名单的目录/文件直接不复制（目录级命中即整棵子树跳过）。 */
const keepFontEntry = (src) => !EXCLUDED_FONT_FAMILIES.has(path.basename(src))

if (!existsSync(prodDir)) {
  console.error(`[sync-assets] 找不到依赖目录：${prodDir}，请先执行 npm install`)
  process.exit(1)
}

for (const name of ['fonts', 'locales']) {
  const src = path.join(prodDir, name)
  const dest = path.join(projectRoot, 'public', name)
  if (!existsSync(src)) {
    console.warn(`[sync-assets] 跳过（依赖内无此目录）：${src}`)
    continue
  }
  rmSync(dest, { recursive: true, force: true })
  mkdirSync(path.dirname(dest), { recursive: true })
  // fonts 需要剔除大体积字体族；locales 是语言包，整体照拷。
  const copyOptions = name === 'fonts' ? { recursive: true, filter: keepFontEntry } : { recursive: true }
  cpSync(src, dest, copyOptions)
  console.log(`[sync-assets] ${name} -> ${path.relative(projectRoot, dest)}`)
  if (name === 'fonts') {
    console.log(
      `[sync-assets] 已跳过 Xiaolai（中文字体，约 12MB，占 fonts 体积 96%）：` +
        '画布与其它字体族不受影响，仅中文手写体回退到系统字体。',
    )
  }
}

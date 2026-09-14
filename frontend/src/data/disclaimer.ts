/**
 * 免责条款正文 —— 设置页「法律 → 免责条款」与首次使用确认弹窗共用的唯一事实源。
 *
 * 修改条款正文后必须同步两处，否则已同意旧版本的用户不会重新看到确认弹窗：
 * 1) 递增本文件 DISCLAIMER_VERSION 与 DISCLAIMER_UPDATED_AT；
 * 2) 同步递增后端 core/disclaimer_consent.py 的 DISCLAIMER_VERSION。
 *
 * 合规红线（勿突破）：
 * 1) 《民法典》第 496–497 条：格式条款不得「不合理地免除或减轻自身责任、
 *    加重对方责任、限制对方主要权利」，否则该条款无效；
 * 2) 《民法典》第 506 条：免除「故意或重大过失造成对方财产损失」责任的
 *    条款无效，故免责表述必须限定在「法律允许的最大范围内」；
 * 3) 本软件为本地工具软件、不提供生成式 AI 服务，模型由使用者自行配置。
 *    该定性决定了数据外发与内容责任的分界（《生成式人工智能服务管理
 *    暂行办法》第 2 条第 3 款），表述不得含糊。
 */

export const DISCLAIMER_VERSION = 'v1.0';
export const DISCLAIMER_UPDATED_AT = '2026-09-11';

/**
 * 校验已同意的版本是否满足当前条款版本要求。
 *
 * 只有已同意版本有效且不低于当前要求版本时返回 true。
 * 一旦条款版本升级（或已同意版本落后），返回 false 触发再次确认。
 */
export function isDisclaimerVersionAccepted(
  acceptedVersion: string | null | undefined,
  requiredVersion: string = DISCLAIMER_VERSION
): boolean {
  if (!acceptedVersion || typeof acceptedVersion !== 'string') return false;
  const acc = acceptedVersion.trim();
  const req = requiredVersion.trim();
  if (!acc || !req) return false;
  if (acc === req) return true;
  const accNorm = acc.replace(/^[vV]/, '');
  const reqNorm = req.replace(/^[vV]/, '');
  if (accNorm === reqNorm) return true;
  try {
    const accParts = accNorm.split('.').map((p) => parseInt(p, 10));
    const reqParts = reqNorm.split('.').map((p) => parseInt(p, 10));
    if (accParts.some(isNaN) || reqParts.some(isNaN)) return accNorm === reqNorm;
    for (let i = 0; i < Math.max(accParts.length, reqParts.length); i++) {
      const a = accParts[i] || 0;
      const r = reqParts[i] || 0;
      if (a > r) return true;
      if (a < r) return false;
    }
    return true;
  } catch {
    return false;
  }
}

export interface DisclaimerClause {
  title: string;
  items: string[];
}

export const DISCLAIMER_CLAUSES: DisclaimerClause[] = [
  {
    title: '一、软件性质与定位',
    items: [
      '星瑶（星瑶Code，以下称「本软件」）是一款在你本地设备运行的 AI 编程辅助工具软件。本软件自身不提供、不运营任何生成式人工智能模型服务。',
      '本软件不接入、不分发也不控制任何模型服务。模型服务由你自行选择、配置并直接与其通信，其可用性、计费、内容策略与数据处理规则由该服务提供者决定并负责。',
      '本软件基于 MIT 许可证开源发布，你可依该许可证自由使用、修改与分发。',
    ],
  },
  {
    title: '二、AI 生成内容',
    items: [
      '本软件调用的大语言模型基于概率机制生成内容，其输出可能不准确、不完整、已过时，或包含错误代码、错误命令与不当表述。本软件不对生成内容的真实性、准确性、完整性、合法性或适用性作出任何保证。',
      '生成内容不构成法律、医疗、财务、税务等任何专业领域意见，不应作为相关决策的唯一依据。',
      '生成内容可能与他人在先作品相似。你在对外使用（尤其是商业使用）前，应自行核查其知识产权状况。',
    ],
  },
  {
    title: '三、你的责任',
    items: [
      '在采纳、执行或分发任何由本软件生成的内容前，你应自行审查、验证与测试，并对最终结果负全部责任。',
      '本软件具备文件读写、命令执行、网页操作与电脑操控等能力，此类操作可能修改或删除本机文件、改变系统状态或产生外部影响。你应在执行前确认操作意图与影响范围，并建议在沙盒、权限限制或隔离环境中使用。',
      '你应自行对重要数据做好备份。因未备份或未复核操作而导致的数据丢失、业务中断等后果，由你自行承担。',
      '你应确保对所配置的模型服务、账号与访问凭据拥有合法使用权限，并妥善保管自身的访问密钥。',
    ],
  },
  {
    title: '四、数据与隐私',
    items: [
      '本软件的配置、会话记录与记忆数据默认存储于你的本地设备，本软件不主动向开发者上传上述数据。',
      '使用过程中，会话内容、代码与附件等将按你的配置直接发送至你所选的模型服务提供者，其收集、存储、使用与跨境传输行为适用该服务提供者的隐私政策。',
      '你应自行评估是否适宜通过所选服务处理敏感信息、商业秘密或个人数据，并自行确保符合相关法律法规与所在单位的管理要求。',
    ],
  },
  {
    title: '五、合规使用义务',
    items: [
      '你不得利用本软件从事违反法律法规的活动，不得生成、传播违法有害信息，不得侵害他人合法权益，不得规避、攻击或破坏计算机信息系统。',
      '你应遵守所在国家或地区的法律法规，以及所使用模型服务提供者的服务条款。因你的使用行为引发的争议、处罚或法律责任，由你自行承担。',
    ],
  },
  {
    title: '六、第三方组件',
    items: [
      '本软件包含第三方开源组件，各组件的著作权与许可条款归其各自权利人所有，并适用其各自许可。',
      '你在分发或改造本软件时，应自行核对并遵守全部相关许可条款（包括署名、许可全文随附等要求）。',
    ],
  },
  {
    title: '七、责任限制',
    items: [
      '在法律允许的最大范围内，本软件的著作权人及贡献者不对因使用或无法使用本软件而造成的任何损失承担责任，包括但不限于数据丢失、设备损坏、业务中断、利润损失及第三方索赔等直接或间接损失。',
      '本软件按「现状」提供，不作任何明示或默示担保，包括但不限于适销性、特定用途适用性及不侵权的担保。',
      '若前述免责事由依适用法律被认定无效或不可执行，本软件的赔偿责任以你为获得本软件实际支付的费用为上限。',
    ],
  },
  {
    title: '八、条款变更',
    items: [
      '本条款可能随软件版本更新而调整，更新后的条款随新版本一并发布，并同步更新上方版本号与日期。',
      '条款更新后你继续使用本软件，视为你已接受更新后的条款。',
    ],
  },
];

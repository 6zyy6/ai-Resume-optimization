const factKinds: Record<string, string> = {
  education: "教育事实",
  experience: "经历事实",
  metric: "数据事实",
  project: "项目事实",
  result: "结果事实",
  skill: "技能事实",
  skills: "技能事实",
};

const sourceTypes: Record<string, string> = {
  fact_candidate_edit: "候选编辑",
  imported_resume: "导入简历",
  question_answer: "原始回答",
  user_confirmation: "用户确认",
  user_edit: "用户编辑",
};

const factStatuses: Record<string, string> = {
  confirmed: "已确认",
  rejected: "不采用",
  unconfirmed: "待确认",
};

const taskTypes: Record<string, string> = {
  account_deletion: "注销账户",
  analyze_intake_answer: "分析经历回答",
  data_export: "导出账户数据",
  file_parse: "解析简历文件",
  generate_intake_draft: "生成基础简历",
  match_resume_to_job: "匹配目标岗位",
  parse_job: "解析岗位要求",
  parse_resume_import: "解析简历文件",
  render_resume_export: "生成 PDF 简历",
  resume_draft: "生成简历",
};

const taskStatuses: Record<string, string> = {
  cancelled: "已取消",
  failed: "任务失败",
  queued: "等待处理",
  running: "处理中",
  succeeded: "已完成",
  waiting_for_user: "等待你的操作",
};

const taskStages: Record<string, string> = {
  analysis: "分析事实",
  completed: "处理完成",
  draft: "生成草稿",
  draft_processing: "生成草稿",
  failed: "处理失败",
  jd_parse_processing: "解析岗位",
  match: "匹配岗位",
  parse: "解析文件",
  queued: "等待调度",
  running: "处理中",
  succeeded: "处理完成",
  suggestions: "生成建议",
};

const riskFlags: Record<string, string> = {
  conflict: "事实存在冲突",
  missing_metric: "缺少量化结果",
  missing_result_source: "缺少结果来源",
  missing_source: "缺少事实来源",
  needs_confirmation: "需要补充确认",
  unsupported_award: "奖项缺少证据",
  unsupported_numeric: "数字缺少证据",
  unsupported_role: "职责缺少证据",
  unsupported_tool: "工具能力缺少证据",
};

const resumeSectionTypes: Record<string, string> = {
  education: "教育模块",
  experience: "经历模块",
  project: "项目模块",
  skills: "技能模块",
  summary: "概览模块",
};

const qualityIssues: Record<string, { message: string; title: string }> = {
  bullet_claim_not_covered: { title: "简历表述需要核对", message: "当前表述与已关联事实不完全一致，请检查内容或重新关联事实。" },
  bullet_fact_cardinality_mismatch: { title: "事实关联需要调整", message: "请为每条独立表述关联一项对应的已确认事实。" },
  bullet_fact_not_confirmed: { title: "事实尚未确认", message: "请先确认已关联事实，再继续使用这条简历表述。" },
  bullet_fact_reference_required: { title: "缺少事实依据", message: "请为这条简历表述关联一项有来源且已确认的事实。" },
  bullet_new_number: { title: "数字缺少依据", message: "当前表述包含事实中没有的数字，请修改内容或补充事实依据。" },
  bullet_responsibility_strength_unsupported: { title: "职责表述需要核对", message: "当前职责描述强于事实依据，请调整措辞或补充事实。" },
  claim_evidence_coverage_required: { title: "事实依据不完整", message: "这条简历表述尚未完整关联事实，请检查后重试。" },
  claim_evidence_duplicate_bullet_id: { title: "简历内容需要刷新", message: "简历内容标识出现冲突，请刷新页面后重试。" },
  claim_evidence_fact_mismatch: { title: "事实依据需要核对", message: "当前简历表述与已确认事实不完全一致，请检查内容或重新关联事实。" },
  claim_evidence_fact_not_confirmed: { title: "事实尚未确认", message: "请先确认已关联事实，再继续使用这条简历表述。" },
  claim_evidence_fact_owner_invalid: { title: "事实无法使用", message: "当前关联事实不可用于这份简历，请重新选择事实。" },
  claim_evidence_fact_source_required: { title: "事实缺少来源", message: "当前关联事实缺少可核验来源，请先补充来源。" },
  claim_evidence_range_invalid: { title: "事实关联位置异常", message: "事实与简历内容的关联位置已失效，请刷新页面后重试。" },
  claim_evidence_range_overlap: { title: "事实关联范围重复", message: "同一段内容存在重复的事实关联，请调整后重试。" },
  claim_evidence_unknown_bullet: { title: "简历内容已经变化", message: "事实关联对应的内容已经变化，请刷新页面后重试。" },
};

const normalized = (value: string) => value.trim().toLowerCase();

export const factKindLabel = (value: string) => factKinds[normalized(value)] ?? "其他事实";
export const factStatusLabel = (value: string) => factStatuses[normalized(value)] ?? "状态待确认";
export const sourceTypeLabel = (value: string) => sourceTypes[normalized(value)] ?? "其他来源";
export const taskTypeLabel = (value: string) => taskTypes[normalized(value)] ?? "系统任务";
export const taskStatusLabel = (value: string) => taskStatuses[normalized(value)] ?? "状态待确认";
export const taskStageLabel = (value: string) => taskStages[normalized(value)] ?? "进度更新";
export const riskFlagLabel = (value: string) => riskFlags[normalized(value)] ?? "需要人工复核";
export const resumeSectionTypeLabel = (value: string) => resumeSectionTypes[normalized(value)] ?? "其他模块";
export const qualityIssueLabel = (value: string) => qualityIssues[normalized(value)] ?? {
  title: "简历内容需要核对",
  message: "系统发现一项需要处理的内容，请检查相关简历表述后重试。",
};
export function resumeTargetLabel(value: string) {
  const match = /^\/sections\/(\d+)\/items\/(\d+)\/text$/.exec(value);
  return match
    ? `简历第 ${Number(match[1]) + 1} 个模块 · 第 ${Number(match[2]) + 1} 条内容`
    : "简历内容位置";
}

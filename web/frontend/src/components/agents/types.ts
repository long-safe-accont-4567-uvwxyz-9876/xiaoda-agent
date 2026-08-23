/**
 * Agent 编辑表单模型。
 * 后端 /agents 序列化对象的宽松超集：JSON 克隆回填时可能缺字段，
 * 故全部可选并保留索引签名以兼容动态属性（tool_count 等展示字段）。
 */
export interface AgentEditModel {
  name?: string
  display_name?: string
  display_name_en?: string
  provider?: string
  model?: string
  base_url?: string
  api_key_env?: string
  route_description?: string
  capabilities?: string[]
  voice_ref?: string | null
  max_turns?: number
  effort?: string
  permission_mode?: string
  memory_scope?: string
  wallpaper?: string
  ack_messages?: string[]
  [key: string]: any
}

/** 权限矩阵分组：[分类名, [工具名, 条目][]] */
export type ToolGroup = [string, Array<[string, any]>]

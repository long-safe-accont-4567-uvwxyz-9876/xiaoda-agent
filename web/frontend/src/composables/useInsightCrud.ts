/**
 * Insight 共享 CRUD 编排：模态框状态 + 六类实体的表单种子构造 + 提交链路。
 * 从 InsightView 原样迁移（2026-08-23 大文件拆分专项）；
 * 校验提示/成功文案/刷新回调时序保持不变。
 */
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  createMemory, updateMemory,
  createNote, updateNote,
  createLearning, updateLearning,
  createInstinct, updateInstinct,
  createKnowledgeEntity, updateKnowledgeEntity,
  createKnowledgeRelation, updateKnowledgeRelation,
} from '../api'
import { t } from '../i18n'
import type { CrudType } from '../components/insight/types'

/** 提交成功后各实体对应的列表刷新函数 */
export interface InsightCrudReloaders {
  memories: () => Promise<void> | void
  notes: () => Promise<void> | void
  learning: () => Promise<void> | void
  knowledge: () => Promise<void> | void
}

export function useInsightCrud(reloaders: InsightCrudReloaders) {
  const message = useMessage()

  const showModal = ref(false)
  const modalType = ref<CrudType | null>(null)
  const editingId = ref<number | string | null>(null)
  const formSeed = ref<Record<string, any>>({})

  const editing = computed(() => !!editingId.value)

  const modalTitle = computed(() => {
    const prefix = editingId.value ? t('insightView.editPrefix') : t('insightView.addPrefix')
    const names: Record<string, string> = {
      memory: t('insightView.namesMemory'), note: t('insightView.namesNote'),
      learning: t('insightView.namesLearning'), instinct: t('insightView.namesInstinct'),
      entity: t('insightView.namesEntity'), relation: t('insightView.namesRelation'),
    }
    return prefix + (names[modalType.value || ''] || '')
  })

  function openAdd(type: CrudType) {
    modalType.value = type
    editingId.value = null
    if (type === 'memory') {
      formSeed.value = { summary: '', importance: 0.5, emotion_label: '' }
    } else if (type === 'note') {
      formSeed.value = { content: '', kind: 'note', tags: '' }
    } else if (type === 'learning') {
      formSeed.value = { summary: '', pattern: '', priority: 'medium' }
    } else if (type === 'instinct') {
      formSeed.value = { content: '', confidence: 0.5 }
    } else if (type === 'entity') {
      formSeed.value = { name: '', kind: '', observations: '' }
    } else if (type === 'relation') {
      formSeed.value = { from: '', to: '', relation: '' }
    }
    showModal.value = true
  }

  function openEdit(type: CrudType, item: Record<string, any>) {
    modalType.value = type
    editingId.value = item.id
    if (type === 'memory') {
      formSeed.value = {
        summary: item.summary || '',
        importance: item.importance ?? 0.5,
        emotion_label: item.emotion_label || '',
      }
    } else if (type === 'note') {
      formSeed.value = {
        content: item.content || '',
        kind: item.kind || 'note',
        tags: item.tags || '',
      }
    } else if (type === 'learning') {
      formSeed.value = {
        summary: item.summary || '',
        pattern: item.pattern || '',
        priority: item.priority || 'medium',
      }
    } else if (type === 'instinct') {
      formSeed.value = {
        content: item.content || item.summary || '',
        confidence: item.confidence ?? 0.5,
      }
    } else if (type === 'entity') {
      editingId.value = item.name
      formSeed.value = {
        name: item.name || '',
        kind: item.kind || '',
        observations: item.observations || '',
      }
    } else if (type === 'relation') {
      editingId.value = item.id
      formSeed.value = {
        from: item.from_entity || '',
        to: item.to_entity || '',
        relation: item.relation_type || '',
      }
    }
    showModal.value = true
  }

  async function handleModalOk(form: Record<string, any>) {
    try {
      if (modalType.value === 'memory') {
        if (!form.summary) { message.warning(t('insightView.inputMemorySummary')); return }
        if (editingId.value) {
          await updateMemory(editingId.value as number, { summary: form.summary, importance: form.importance, emotion_label: form.emotion_label })
        } else {
          await createMemory({ summary: form.summary, importance: form.importance, emotion_label: form.emotion_label })
        }
        await reloaders.memories()
      } else if (modalType.value === 'note') {
        if (!form.content) { message.warning(t('insightView.inputNoteContent')); return }
        if (editingId.value) {
          await updateNote(editingId.value as number, { content: form.content, kind: form.kind, tags: form.tags })
        } else {
          await createNote({ content: form.content, kind: form.kind, tags: form.tags })
        }
        await reloaders.notes()
      } else if (modalType.value === 'learning') {
        if (!form.summary) { message.warning(t('insightView.inputLearningSummary')); return }
        if (editingId.value) {
          await updateLearning(editingId.value as number, { summary: form.summary, pattern: form.pattern, priority: form.priority })
        } else {
          await createLearning({ summary: form.summary, pattern: form.pattern, priority: form.priority })
        }
        await reloaders.learning()
      } else if (modalType.value === 'instinct') {
        if (!form.content) { message.warning(t('insightView.inputInstinctContent')); return }
        if (editingId.value) {
          await updateInstinct(editingId.value as number, { content: form.content, confidence: form.confidence })
        } else {
          await createInstinct({ content: form.content, confidence: form.confidence })
        }
        await reloaders.learning()
      } else if (modalType.value === 'entity') {
        if (!form.name) { message.warning(t('insightView.inputEntityName')); return }
        if (editingId.value) {
          await updateKnowledgeEntity(editingId.value as string, { kind: form.kind, observations: form.observations })
        } else {
          await createKnowledgeEntity({ name: form.name, kind: form.kind, observations: form.observations })
        }
        await reloaders.knowledge()
      } else if (modalType.value === 'relation') {
        if (!form.from || !form.to || !form.relation) { message.warning(t('insightView.inputRelationInfo')); return }
        if (editingId.value) {
          await updateKnowledgeRelation(editingId.value as string, { relation: form.relation })
        } else {
          await createKnowledgeRelation({ from: form.from, to: form.to, relation: form.relation })
        }
        await reloaders.knowledge()
      }
      showModal.value = false
      message.success(editingId.value ? t('insightView.updated') : t('insightView.created'))
    } catch (e: any) {
      message.error(e.message)
    }
  }

  return {
    showModal,
    modalType,
    editingId,
    formSeed,
    editing,
    modalTitle,
    openAdd,
    openEdit,
    handleModalOk,
  }
}

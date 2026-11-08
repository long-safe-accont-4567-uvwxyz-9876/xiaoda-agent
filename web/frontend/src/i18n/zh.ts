/* zh locale: aggregated from ./zh/*.ts domain modules; keys unchanged */
import base from './zh/base'
import views from './zh/views'
import integrations from './zh/integrations'
import pages from './zh/pages'

export default {
  ...base,
  ...views,
  ...integrations,
  ...pages,
} as const

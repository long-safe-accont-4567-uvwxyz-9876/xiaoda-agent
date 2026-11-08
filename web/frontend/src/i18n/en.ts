/* en locale: aggregated from ./en/*.ts domain modules; keys unchanged */
import base from './en/base'
import views from './en/views'
import integrations from './en/integrations'
import pages from './en/pages'

export default {
  ...base,
  ...views,
  ...integrations,
  ...pages,
} as const

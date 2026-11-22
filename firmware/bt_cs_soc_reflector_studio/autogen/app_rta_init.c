#include "sl_component_catalog.h"
#ifdef SL_CATALOG_KERNEL_PRESENT
#include "app_rta_internal.h"
#else // SL_CATALOG_KERNEL_PRESENT
#include "app_rta_internal_bm.h"
#endif // SL_CATALOG_KERNEL_PRESENT
#include "cs_ras_server_internal.h"

void app_rta_init_contributors(void)
{
  cs_ras_server_rta_init();
}

void app_rta_ready(void)
{
  cs_ras_server_rta_ready();
}

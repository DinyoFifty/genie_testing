#include "sl_bt_peer_manager_common.h"

#include "app.h"

void sl_bt_peer_manager_on_event(sl_bt_peer_manager_evt_type_t* event)
{
  ble_peer_manager_on_event_reflector(event);

}

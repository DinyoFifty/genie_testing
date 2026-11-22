/***************************************************************************//**
 * @file
 * @brief
 *******************************************************************************
 * # License
 * <b>Copyright 2023 Silicon Laboratories Inc. www.silabs.com</b>
 *******************************************************************************
 *
 * SPDX-License-Identifier: Zlib
 *
 * The licensor of this software is Silicon Laboratories Inc.
 *
 * This software is provided 'as-is', without any express or implied
 * warranty. In no event will the authors be held liable for any damages
 * arising from the use of this software.
 *
 * Permission is granted to anyone to use this software for any purpose,
 * including commercial applications, and to alter it and redistribute it
 * freely, subject to the following restrictions:
 *
 * 1. The origin of this software must not be misrepresented; you must not
 *    claim that you wrote the original software. If you use this software
 *    in a product, an acknowledgment in the product documentation would be
 *    appreciated but is not required.
 * 2. Altered source versions must be plainly marked as such, and must not be
 *    misrepresented as being the original software.
 * 3. This notice may not be removed or altered from any source distribution.
 *
 ******************************************************************************/

#ifndef SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_H
#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_H

#ifdef __cplusplus
extern "C" {
#endif

// <<< Use Configuration Wizard in Context Menu >>>

// Genie BRD2606A isolated-antenna testing. Change this to 1 or 2 on both boards.
#define GENIE_TEST_ANTENNA                               1

// Keep calibration values associated with each physical antenna.
#define GENIE_ANT1_WIRELESS_OFFSET_CM                    0
#define GENIE_ANT2_WIRELESS_OFFSET_CM                    0
#define GENIE_ANT1_WIRED_OFFSET_CM                       25
#define GENIE_ANT2_WIRED_OFFSET_CM                       25

#if GENIE_TEST_ANTENNA == 1
#define GENIE_SELECTED_WIRELESS_OFFSET_CM GENIE_ANT1_WIRELESS_OFFSET_CM
#define GENIE_SELECTED_WIRED_OFFSET_CM    GENIE_ANT1_WIRED_OFFSET_CM
#elif GENIE_TEST_ANTENNA == 2
#define GENIE_SELECTED_WIRELESS_OFFSET_CM GENIE_ANT2_WIRELESS_OFFSET_CM
#define GENIE_SELECTED_WIRED_OFFSET_CM    GENIE_ANT2_WIRED_OFFSET_CM
#else
#error "GENIE_TEST_ANTENNA must be 1 or 2"
#endif

// <h> CS Antenna Offset Configuration
// <o SL_RAIL_UTIL_CS_ANTENNA_COUNT> Number of available antennas <1..4>
// <d> 2
#define SL_RAIL_UTIL_CS_ANTENNA_COUNT                     2

// <o SL_RAIL_UTIL_CS_ANTENNA_CONFIG_TYPE> Antenna offset configuration type
// <SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_WIRELESS=> Wireless antenna connection
// <SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_WIRED=> Wired antenna connection
// <d> SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_WIRELESS
#define SL_RAIL_UTIL_CS_ANTENNA_CONFIG_TYPE               SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_TYPE_WIRELESS

// <a.4 SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM> Antenna wireless offset array in centimeters <-32768..32767>
// <i> If less than 4 antenna paths are available, 0 must be supplied for the non-existent elements.
// <d> { 56, 67, 0, 0 }
#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRELESS_CM        { GENIE_ANT1_WIRELESS_OFFSET_CM, GENIE_ANT2_WIRELESS_OFFSET_CM, 0, 0 }

// <a.4 SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRED_CM> Antenna wired offset array in centimeters <-32768..32767>
// <i> If less than 4 antenna paths are available, 0 must be supplied for the non-existent elements.
// <d> { 25, 25, 0, 0 }
#define SL_RAIL_UTIL_CS_ANTENNA_OFFSET_WIRED_CM           { GENIE_ANT1_WIRED_OFFSET_CM, GENIE_ANT2_WIRED_OFFSET_CM, 0, 0 }
// </h>

// <<< end of configuration section >>>

#ifdef __cplusplus
}
#endif

#endif // SL_RAIL_UTIL_CS_ANTENNA_OFFSET_CONFIG_H


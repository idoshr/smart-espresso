# espresso



## Installation

```bash
pip install espresso
```

## Usage

### Configuration
| VALUE         | Description               | Example            |
|---------------|---------------------------|--------------------|
| HA_ENABLE     | Home Assistant Enabled    | true / false       |
| HA_URL        | Home Assistant Url        | http://192.168.1.1 |
| HA_TOKEN      | Home Assistant Api token  |                    |
| HA_VERIFY_SSL | Home Assistant verify SSL | true / false       |

```bash
python main.py
```


### Prerequisite list

| Items | description                                                                                                                      | url                               | notes                                                                           |
|-------|----------------------------------------------------------------------------------------------------------------------------------|-----------------------------------|---------------------------------------------------------------------------------|
| 1     | Raspberry Pi 4 Model B 2GB RAM                                                                                                   |                                   |                                                                                 |
| 1     | MCP3008-I/P MCP3008 DIP-16                                                                                                       | https://a.aliexpress.com/_olcJc4g |                                                                                 |
| 2     | Pneumatic Plumbing Brass Pipe Fitting Male/Female Thread 1/8" 1/4" 3/8" 1/2" BSP Tee Type Copper Fittings Water Oil Gas Adapter  | https://a.aliexpress.com/_okOIGjW | Choose the F-F-M 1/8"                                                           |
| 1     | Water Oil Fuel Gas Air Pressure Sensor Transducer (PT1/4 G1/2 G1/8) 5-12V 0.5-4.5V 0-300Bar Gauge Optional Consumer Electronics  | https://a.aliexpress.com/_omToNFi | Choose the 0-0.5Mpa G1-8 and write to the seller that you need the 3.3v version |   
| 1     | Water Oil Fuel Gas Air Pressure Sensor Transducer (PT1/4 G1/2 G1/8) 5-12V 0.5-4.5V 0-300Bar Gauge Optional Consumer Electronics  | https://a.aliexpress.com/_omToNFi | Choose the 0-2Mpa G1-8 and write to the seller that you need the 3.3v version   |          
| 1     | 60 Values 1/4W 1％ Metal Film Resistors Kit 300Pcs 1 ohm - 4.7M ohm 1/4 Watt W High Precision MF Resistance Set Assortment        | https://a.aliexpress.com/_oBZ1yNa |                                                                                 |
| 1     | 530pcs/Set Polyolefin Shrinking Assorted Heat Shrink Tube Wire Cable Insulated Sleeving Tubing Set 2:1 Waterproof Pipe Sleeve    | https://a.aliexpress.com/_okRXZ5m |                                                                                 |
| 1     | DIY Prototype Raspberry Pi Expansion Board PCB Shield  Compatible for Raspberry Pi 4B/3B/3B  Board For DIY Starter Projects      | https://a.aliexpress.com/_opgseK8 |                                                                                 |
| 1     | 1.3" OLED Display Module White/Blue Color Drive Chip SH1106 128X64 1.3 inch OLED LCD LED IIC I2C Communicate For ArduinoProjects | https://a.aliexpress.com/_oEzEfpA | Nice but too small                                                              |

### Wiring diagram
